/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_commands.hh"
#include "agent_context.hh"
#include "agent_production.hh"

#include <cmath>
#include <cstring>
#include <filesystem>
#include <limits>
#include <list>
#include <stdexcept>
#include <vector>

#include "BKE_context.hh"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_report.hh"
#include "BKE_scene.hh"
#include "BLI_listbase.hh"
#include "BLI_threads.hh"
#include "DNA_layer_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"
#include "DNA_windowmanager_types.h"
#include "ED_object.hh"
#include "ED_util.hh"
#include "MEM_guardedalloc.h"
#include "RNA_access.hh"
#include "RNA_path.hh"
#include "RNA_prototypes.hh"
#include "WM_api.hh"
#include "WM_types.hh"

namespace blender::agent {

using JSON = CommandJSON;

static void require(bool condition, const std::string &message)
{
  if (!condition) {
    throw std::runtime_error(message);
  }
}

struct Reports {
  ReportList list;
  Reports()
  {
    BKE_reports_init(&list, RPT_STORE | RPT_PRINT_HANDLED_BY_OWNER);
  }
  ~Reports()
  {
    BKE_reports_free(&list);
  }
  JSON get() const
  {
    JSON result = JSON::array();
    for (const Report &report : list.list) {
      result.push_back({{"type", report.typestr}, {"message", report.message}});
    }
    return result;
  }
  void check()
  {
    if (BKE_reports_contain(&list, RPT_ERROR)) {
      throw std::runtime_error(get().dump());
    }
  }
};

CommandProperty command_resolve(bContext *C, const std::string &path)
{
  PointerRNA root = RNA_main_pointer_create(CTX_data_main(C));
  CommandProperty target;
  /* Preserve final pointer properties (including null) for assignment. Collection
   * lookups, on the other hand, must resolve to the item, not the whole collection. */
  if (RNA_path_resolve_property_full(
          &root, path.c_str(), &target.pointer, &target.property, &target.index) &&
      RNA_property_type(target.property) == PROP_POINTER)
  {
    return target;
  }
  require(
      RNA_path_resolve_full(&root, path.c_str(), &target.pointer, &target.property, &target.index),
      "Cannot resolve native RNA path: " + path);
  return target;
}

static JSON pointer_json(bContext *C, const PointerRNA &ptr)
{
  if (!ptr.data) {
    return nullptr;
  }
  JSON result = {{"type", RNA_struct_identifier(ptr.type)}};
  PointerRNA mutable_ptr = ptr;
  if (PropertyRNA *name = RNA_struct_name_property(ptr.type)) {
    result["name"] = RNA_property_string_get(&mutable_ptr, name);
  }
  if (ptr.owner_id) {
    ID *owner = nullptr;
    const auto suffix = RNA_path_from_real_ID_to_struct(CTX_data_main(C), &ptr, &owner);
    if (!owner || !suffix) {
      return result;
    }
    PointerRNA root = RNA_main_pointer_create(CTX_data_main(C));
    PropertyRNA *properties = RNA_struct_iterator_property(root.type);
    RNA_PROP_BEGIN (&root, property_ptr, properties) {
      PropertyRNA *property = static_cast<PropertyRNA *>(property_ptr.data);
      if (RNA_property_type(property) != PROP_COLLECTION) {
        continue;
      }
      PointerRNA candidate;
      if (RNA_property_collection_lookup_string(&root, property, owner->name + 2, &candidate) &&
          candidate.data == owner)
      {
        std::string path = std::string(RNA_property_identifier(property)) + "[" +
                           JSON(owner->name + 2).dump() + "]";
        if (!suffix->empty()) {
          path += "." + *suffix;
        }
        PropertyRNA *resolved_property = nullptr;
        if (RNA_path_resolve(&root, path.c_str(), &candidate, &resolved_property) &&
            !resolved_property && candidate.data == ptr.data)
        {
          result["path"] = path;
        }
        break;
      }
    }
    RNA_PROP_END;
  }
  return result;
}

static int enum_value(bContext *C, PointerRNA *ptr, PropertyRNA *prop, const JSON &value)
{
  const bool flags = RNA_property_flag(prop) & PROP_ENUM_FLAG;
  require(flags ? value.is_array() : value.is_string(),
          "Enum requires an identifier (or an array of identifiers for flags)");
  const EnumPropertyItem *items;
  int count;
  bool free_items;
  RNA_property_enum_items(C, ptr, prop, &items, &count, &free_items);
  int result = 0;
  bool valid = true;
  const JSON values = flags ? value : JSON::array({value});
  for (const JSON &entry : values) {
    bool found = false;
    for (int i = 0; i < count; i++) {
      if (items[i].identifier[0] && entry == items[i].identifier) {
        result |= items[i].value;
        found = true;
        break;
      }
    }
    valid &= found;
  }
  if (free_items) {
    MEM_delete(items);
  }
  require(valid, "Unknown enum identifier for " + std::string(RNA_property_identifier(prop)));
  return result;
}

static JSON enum_json(bContext *C, PointerRNA *ptr, PropertyRNA *prop, int value)
{
  const EnumPropertyItem *items;
  int count;
  bool free_items;
  RNA_property_enum_items(C, ptr, prop, &items, &count, &free_items);
  const bool flags = RNA_property_flag(prop) & PROP_ENUM_FLAG;
  JSON result = flags ? JSON::array() : JSON(value);
  for (int i = 0; i < count; i++) {
    if (!items[i].identifier[0]) {
      continue;
    }
    if (flags && items[i].value && (value & items[i].value) == items[i].value) {
      result.push_back(items[i].identifier);
    }
    else if (!flags && value == items[i].value) {
      result = items[i].identifier;
      break;
    }
  }
  if (free_items) {
    MEM_delete(items);
  }
  return result;
}

CommandJSON command_get(bContext *C, const CommandProperty &target)
{
  PointerRNA ptr = target.pointer;
  PropertyRNA *prop = target.property;
  if (!prop) {
    return pointer_json(C, ptr);
  }
  const int length = RNA_property_array_length(&ptr, prop);
  if (length && target.index < 0) {
    JSON result = JSON::array();
    for (int i = 0; i < length; i++) {
      result.push_back(command_get(C, {ptr, prop, i}));
    }
    return result;
  }
  switch (RNA_property_type(prop)) {
    case PROP_BOOLEAN:
      return target.index < 0 ? RNA_property_boolean_get(&ptr, prop) :
                                RNA_property_boolean_get_index(&ptr, prop, target.index);
    case PROP_INT:
      return target.index < 0 ? RNA_property_int_get(&ptr, prop) :
                                RNA_property_int_get_index(&ptr, prop, target.index);
    case PROP_FLOAT:
      return target.index < 0 ? RNA_property_float_get(&ptr, prop) :
                                RNA_property_float_get_index(&ptr, prop, target.index);
    case PROP_STRING:
      return RNA_property_string_get(&ptr, prop);
    case PROP_ENUM:
      return enum_json(C, &ptr, prop, RNA_property_enum_get(&ptr, prop));
    case PROP_POINTER:
      return pointer_json(C, RNA_property_pointer_get(&ptr, prop));
    case PROP_COLLECTION: {
      JSON result = JSON::array();
      RNA_PROP_BEGIN (&ptr, item, prop) {
        result.push_back(pointer_json(C, item));
      }
      RNA_PROP_END;
      return result;
    }
  }
  throw std::runtime_error("Unsupported RNA property type");
}

static PointerRNA pointer_value(bContext *C,
                                PointerRNA *owner,
                                PropertyRNA *prop,
                                const JSON &value)
{
  if (value.is_null()) {
    require(!(RNA_property_flag(prop) & PROP_NEVER_NULL), "RNA pointer cannot be null");
    return {};
  }
  require(value.is_object() && value.contains("path") && value["path"].is_string(),
          "RNA pointer requires {\"path\":\"native RNA path\"}");
  CommandProperty target = command_resolve(C, value["path"]);
  if (target.property && RNA_property_type(target.property) == PROP_POINTER) {
    target.pointer = RNA_property_pointer_get(&target.pointer, target.property);
    target.property = nullptr;
  }
  require(!target.property && target.pointer.data, "Pointer path must resolve to a struct");
  require(RNA_struct_is_a(target.pointer.type, RNA_property_pointer_type(owner, prop)),
          "RNA pointer type mismatch for " + std::string(RNA_property_identifier(prop)));
  require(!(RNA_property_flag(prop) & PROP_ID_SELF_CHECK) ||
              owner->owner_id != target.pointer.owner_id,
          "RNA pointer cannot refer to its own ID");
  return target.pointer;
}

static void validate_scalar(bContext *C, PointerRNA *ptr, PropertyRNA *prop, const JSON &value)
{
  switch (RNA_property_type(prop)) {
    case PROP_BOOLEAN:
      require(value.is_boolean(), "RNA boolean requires true or false");
      break;
    case PROP_INT: {
      int low, high;
      RNA_property_int_range(ptr, prop, &low, &high);
      require(value.is_number_integer() && value >= low && value <= high,
              "RNA integer outside range or wrong type: " +
                  std::string(RNA_property_identifier(prop)));
      break;
    }
    case PROP_FLOAT: {
      float low, high;
      RNA_property_float_range(ptr, prop, &low, &high);
      require(
          value.is_number() && std::isfinite(value.get<double>()) && value >= low && value <= high,
          "RNA number outside range or wrong type: " + std::string(RNA_property_identifier(prop)));
      break;
    }
    case PROP_STRING: {
      require(value.is_string(), "RNA string requires a string");
      const std::string &text = value.get_ref<const std::string &>();
      const int limit = RNA_property_string_maxlength(prop);
      require(text.find('\0') == std::string::npos && (!limit || text.size() < size_t(limit)),
              "RNA string contains NUL or exceeds maximum length");
      break;
    }
    case PROP_ENUM:
      enum_value(C, ptr, prop, value);
      break;
    case PROP_POINTER:
      pointer_value(C, ptr, prop, value);
      break;
    default:
      throw std::runtime_error("RNA collections are modified through data call, not assignment");
  }
}

void command_set(bContext *C, const CommandProperty &target, const JSON &value)
{
  PointerRNA ptr = target.pointer;
  PropertyRNA *prop = target.property;
  require(prop != nullptr, "Assignment requires an RNA property, not a struct");
  const char *reason = nullptr;
  require(RNA_property_editable_info(&ptr, prop, &reason),
          std::string("RNA property is read-only: ") +
              (reason ? reason : RNA_property_identifier(prop)));
  const int length = RNA_property_array_length(&ptr, prop);
  if (length && target.index < 0) {
    require(value.is_array() && value.size() == size_t(length), "RNA array length mismatch");
    for (int i = 0; i < length; i++) {
      require(RNA_property_editable_index(&ptr, prop, i), "RNA array component is read-only");
      validate_scalar(C, &ptr, prop, value[i]);
    }
    for (int i = 0; i < length; i++) {
      command_set(C, {ptr, prop, i}, value[i]);
    }
    return;
  }
  require(target.index < 0 ||
              (target.index < length && RNA_property_editable_index(&ptr, prop, target.index)),
          "RNA array index invalid or read-only");
  validate_scalar(C, &ptr, prop, value);
  switch (RNA_property_type(prop)) {
    case PROP_BOOLEAN:
      if (target.index < 0)
        RNA_property_boolean_set(&ptr, prop, value.get<bool>());
      else
        RNA_property_boolean_set_index(&ptr, prop, target.index, value.get<bool>());
      break;
    case PROP_INT:
      if (target.index < 0)
        RNA_property_int_set(&ptr, prop, value.get<int>());
      else
        RNA_property_int_set_index(&ptr, prop, target.index, value.get<int>());
      break;
    case PROP_FLOAT:
      if (target.index < 0)
        RNA_property_float_set(&ptr, prop, value.get<float>());
      else
        RNA_property_float_set_index(&ptr, prop, target.index, value.get<float>());
      break;
    case PROP_STRING:
      RNA_property_string_set(&ptr, prop, value.get_ref<const std::string &>().c_str());
      break;
    case PROP_ENUM:
      RNA_property_enum_set(&ptr, prop, enum_value(C, &ptr, prop, value));
      break;
    case PROP_POINTER: {
      Reports reports;
      PointerRNA pointer = pointer_value(C, &ptr, prop, value);
      require(!pointer.data || RNA_property_pointer_poll(&ptr, prop, &pointer),
              "RNA pointer rejected by property's type/context poll");
      RNA_property_pointer_set(&ptr, prop, pointer, &reports.list);
      reports.check();
      break;
    }
    default:
      break;
  }
  RNA_property_update(C, &ptr, prop);
}

static Object *find_object(bContext *C, const std::string &name)
{
  Object *object = reinterpret_cast<Object *>(
      BKE_libblock_find_name(CTX_data_main(C), ID_OB, name.c_str()));
  require(object != nullptr, "Object not found: " + name);
  return object;
}

void command_select(bContext *C,
                    const JSON &objects,
                    const std::string &active,
                    const std::string &mode)
{
  require(objects.is_array(), "objects must be an array of names");
  context_ensure(C);
  Scene *scene = CTX_data_scene(C);
  ViewLayer *layer = CTX_data_view_layer(C);
  BKE_view_layer_synced_ensure(*CTX_data_main(C), scene, layer);
  std::vector<Base *> bases;
  for (const JSON &name : objects) {
    require(name.is_string(), "Object name must be a string");
    Base *base = BKE_view_layer_base_find(layer, find_object(C, name));
    require(base != nullptr, "Object is not in the active view layer: " + name.get<std::string>());
    bases.push_back(base);
  }
  Base *active_base = bases.empty() ? nullptr : bases.front();
  if (!active.empty()) {
    active_base = BKE_view_layer_base_find(layer, find_object(C, active));
    require(active_base && std::find(bases.begin(), bases.end(), active_base) != bases.end(),
            "Active object must belong to objects selection");
  }
  if (Object *object = CTX_data_active_object(C); object && object->mode != OB_MODE_OBJECT) {
    command_operator(C, "OBJECT_OT_mode_set", {{"mode", "OBJECT"}});
  }
  for (Base &base : *BKE_view_layer_object_bases_unsynced_get(layer)) {
    ed::object::base_select(&base, ed::object::BA_DESELECT);
  }
  for (Base *base : bases) {
    ed::object::base_select(base, ed::object::BA_SELECT);
    require(base->flag & BASE_SELECTED, "Object cannot be selected in this view layer");
  }
  ed::object::base_activate(C, active_base);
  if (mode != "OBJECT") {
    command_operator(C, "OBJECT_OT_mode_set", {{"mode", mode}});
  }
}

static wmOperatorType *operator_type(const std::string &name)
{
  wmOperatorType *type = WM_operatortype_find(name.c_str(), true);
  require(type != nullptr, "Unknown operator: " + name);
  require(type->rna_ext.data == nullptr,
          "Python-defined operator requires the explicit exec extension: " + name);
  for (const wmOperatorTypeMacro &macro : type->macro) {
    operator_type(macro.idname);
  }
  return type;
}

CommandJSON command_operator(bContext *C, const std::string &name, const JSON &properties)
{
  require(properties.is_object(), "Operator properties must be an object");
  context_ensure(C);
  wmOperatorType *type = operator_type(name);
  PointerRNA ptr = WM_operator_properties_create_ptr(type);
  struct FreeProperties {
    PointerRNA *ptr;
    ~FreeProperties()
    {
      WM_operator_properties_free(ptr);
    }
  } free_properties{&ptr};
  for (const auto &[key, value] : properties.items()) {
    PropertyRNA *prop = RNA_struct_find_property(&ptr, key.c_str());
    require(prop != nullptr, "Unknown operator property: " + key);
    command_set(C, {ptr, prop, -1}, value);
  }
  if (!WM_operator_poll_context(C, type, wm::OpCallContext::ExecDefault)) {
    bool free_message = false;
    const char *message = CTX_wm_operator_poll_msg_get(C, &free_message);
    const std::string error = "Operator poll failed: " + name +
                              (message ? ": " + std::string(message) : "");
    CTX_wm_operator_poll_msg_clear(C);
    if (free_message)
      MEM_delete(message);
    throw std::runtime_error(error);
  }
  Reports reports;
  /* Despite its historical name, this is the native EXEC entry with caller-owned reports.
   * It does not invoke Python. The operator type above must be native. */
  const wmOperatorStatus status = WM_operator_call_py(
      C, type, wm::OpCallContext::ExecDefault, &ptr, &reports.list, false);
  reports.check();
  require(status & OPERATOR_FINISHED,
          "Operator did not finish: " + name + " " + reports.get().dump());
  return {{"status", "FINISHED"}, {"reports", reports.get()}};
}

static JSON property_schema(bContext *C, PointerRNA *ptr, PropertyRNA *prop)
{
  const char *type = "unknown";
  switch (RNA_property_type(prop)) {
    case PROP_BOOLEAN:
      type = "boolean";
      break;
    case PROP_INT:
      type = "integer";
      break;
    case PROP_FLOAT:
      type = "number";
      break;
    case PROP_STRING:
      type = "string";
      break;
    case PROP_ENUM:
      type = "enum";
      break;
    case PROP_POINTER:
      type = "pointer";
      break;
    case PROP_COLLECTION:
      type = "collection";
      break;
  }
  JSON result = {{"name", RNA_property_identifier(prop)},
                 {"description", RNA_property_ui_description(prop)},
                 {"type", type},
                 {"array_length", RNA_property_array_length(ptr, prop)},
                 {"readonly", !RNA_property_editable(ptr, prop)}};
  if (RNA_property_type(prop) == PROP_INT) {
    int low, high;
    RNA_property_int_range(ptr, prop, &low, &high);
    result["minimum"] = low;
    result["maximum"] = high;
  }
  if (RNA_property_type(prop) == PROP_FLOAT) {
    float low, high;
    RNA_property_float_range(ptr, prop, &low, &high);
    result["minimum"] = low;
    result["maximum"] = high;
  }
  if (RNA_property_type(prop) == PROP_ENUM) {
    const EnumPropertyItem *items;
    int count;
    bool free_items;
    RNA_property_enum_items(C, ptr, prop, &items, &count, &free_items);
    result["enum"] = JSON::array();
    for (int i = 0; i < count; i++) {
      if (items[i].identifier[0])
        result["enum"].push_back(items[i].identifier);
    }
    if (free_items)
      MEM_delete(items);
  }
  return result;
}

static JSON operator_describe(bContext *C, const std::string &name)
{
  wmOperatorType *type = operator_type(name);
  PointerRNA ptr = WM_operator_properties_create_ptr(type);
  JSON properties = JSON::array();
  RNA_PROP_BEGIN (&ptr, item, RNA_struct_iterator_property(ptr.type)) {
    PropertyRNA *prop = static_cast<PropertyRNA *>(item.data);
    if (std::strcmp(RNA_property_identifier(prop), "rna_type") != 0) {
      properties.push_back(property_schema(C, &ptr, prop));
    }
  }
  RNA_PROP_END;
  WM_operator_properties_free(&ptr);
  return {{"name", type->idname},
          {"description", type->description ? type->description : ""},
          {"properties", properties}};
}

/* Function argument storage is owned for the complete call. RNA owns dynamic arrays in
 * ParameterList; stable list entries hold strings and PointerRNA references. */
static JSON data_call(bContext *C, const std::string &path, const JSON &arguments)
{
  require(arguments.is_object(), "RNA function arguments must be an object");
  const size_t split = path.rfind('.');
  require(split != std::string::npos, "Function path must include its owner");
  CommandProperty target = command_resolve(C, path.substr(0, split));
  PointerRNA ptr = target.pointer;
  if (target.property && RNA_property_type(target.property) == PROP_POINTER) {
    ptr = RNA_property_pointer_get(&ptr, target.property);
    require(ptr.data != nullptr, "RNA function owner is null");
    target.property = nullptr;
  }
  if (target.property) {
    require(RNA_property_type(target.property) == PROP_COLLECTION,
            "Function owner must be a struct or collection");
    const auto collection = RNA_property_collection_type_get(&ptr, target.property);
    require(collection.has_value(), "Collection has no native methods");
    ptr = *collection;
  }
  FunctionRNA *function = RNA_struct_find_function(ptr.type, path.substr(split + 1).c_str());
  require(function && RNA_function_defined(function), "Unknown native RNA function: " + path);
  PointerRNA function_ptr = RNA_pointer_create_discrete(ptr.owner_id, RNA_Function, function);
  ParameterList parameters;
  RNA_parameter_list_create(&parameters, &ptr, function);
  struct FreeParameters {
    ParameterList *parameters;
    ~FreeParameters()
    {
      RNA_parameter_list_free(parameters);
    }
  } free_parameters{&parameters};
  std::list<std::string> strings;
  std::list<PointerRNA> pointers;
  for (const auto &[key, value] : arguments.items()) {
    PropertyRNA *prop = RNA_function_find_parameter(&ptr, function, key.c_str());
    require(prop && !(RNA_parameter_flag(prop) & PARM_OUTPUT),
            "Unknown RNA input argument: " + key);
    ParameterIterator input;
    RNA_parameter_list_begin(&parameters, &input);
    while (input.valid && input.parm != prop) {
      RNA_parameter_list_next(&input);
    }
    void *data = input.data;
    RNA_parameter_list_end(&input);
    const int flag = RNA_property_flag(prop);
    int length = RNA_property_array_length(&function_ptr, prop);
    const bool array = RNA_property_array_check(prop);
    if (array) {
      require(value.is_array(), "RNA array argument requires an array: " + key);
      if (flag & PROP_DYNAMIC) {
        require(value.size() <= size_t(std::numeric_limits<int>::max()), "RNA array too large");
        length = int(value.size());
        RNA_parameter_dynamic_length_set(&parameters, prop, length);
        auto *allocation = static_cast<ParameterDynAlloc *>(data);
        const PropertyType type = RNA_property_type(prop);
        require(type == PROP_BOOLEAN || type == PROP_INT || type == PROP_FLOAT,
                "Unsupported RNA dynamic array element type");
        const size_t size = type == PROP_BOOLEAN ? sizeof(bool) :
                            type == PROP_INT     ? sizeof(int) :
                                                   sizeof(float);
        allocation->array = MEM_new_zeroed(size * length, "native RNA argument");
        data = allocation->array;
      }
      require(value.size() == size_t(length), "RNA argument array length mismatch: " + key);
    }
    const JSON values = array ? value : JSON::array({value});
    for (size_t i = 0; i < values.size(); i++) {
      const JSON &entry = values[i];
      validate_scalar(C, &function_ptr, prop, entry);
      switch (RNA_property_type(prop)) {
        case PROP_BOOLEAN:
          static_cast<bool *>(data)[i] = entry.get<bool>();
          break;
        case PROP_INT:
          static_cast<int *>(data)[i] = entry.get<int>();
          break;
        case PROP_FLOAT:
          static_cast<float *>(data)[i] = entry.get<float>();
          break;
        case PROP_ENUM:
          static_cast<int *>(data)[i] = enum_value(C, &function_ptr, prop, entry);
          break;
        case PROP_STRING: {
          strings.push_back(entry.get<std::string>());
          if (flag & PROP_DYNAMIC) {
            RNA_parameter_dynamic_length_set(&parameters, prop, int(strings.back().size()));
            auto *allocation = static_cast<ParameterDynAlloc *>(data);
            allocation->array = MEM_new_zeroed(strings.back().size() + 1, "native RNA string");
            std::memcpy(allocation->array, strings.back().c_str(), strings.back().size() + 1);
          }
          else if (flag & PROP_THICK_WRAP) {
            std::memcpy(data, strings.back().c_str(), strings.back().size() + 1);
          }
          else {
            *static_cast<const char **>(data) = strings.back().c_str();
          }
          break;
        }
        case PROP_POINTER: {
          pointers.push_back(pointer_value(C, &function_ptr, prop, entry));
          if (RNA_parameter_flag(prop) & PARM_RNAPTR) {
            if (flag & PROP_THICK_WRAP)
              *static_cast<PointerRNA *>(data) = pointers.back();
            else
              *static_cast<PointerRNA **>(data) = entry.is_null() ? nullptr : &pointers.back();
          }
          else
            *static_cast<void **>(data) = pointers.back().data;
          break;
        }
        default:
          throw std::runtime_error("Unsupported RNA function argument type: " + key);
      }
    }
  }
  ParameterIterator iter;
  RNA_parameter_list_begin(&parameters, &iter);
  for (; iter.valid; RNA_parameter_list_next(&iter)) {
    const int flags = RNA_parameter_flag(iter.parm);
    require(!(flags & PARM_REQUIRED) || (flags & PARM_OUTPUT) ||
                arguments.contains(RNA_property_identifier(iter.parm)),
            "Missing required RNA argument: " + std::string(RNA_property_identifier(iter.parm)));
  }
  RNA_parameter_list_end(&iter);
  Reports reports;
  const int error = RNA_function_call(C, &reports.list, &ptr, function, &parameters);
  reports.check();
  require(error == 0, "RNA function call failed: " + path);
  JSON outputs = JSON::object();
  RNA_parameter_list_begin(&parameters, &iter);
  for (; iter.valid; RNA_parameter_list_next(&iter)) {
    PropertyRNA *prop = iter.parm;
    if (!(RNA_parameter_flag(prop) & PARM_OUTPUT))
      continue;
    void *data = iter.data;
    const int flag = RNA_property_flag(prop);
    const bool array = RNA_property_array_check(prop);
    int length = array ? RNA_property_array_length(&function_ptr, prop) : 1;
    if (flag & PROP_DYNAMIC) {
      auto *allocation = static_cast<ParameterDynAlloc *>(data);
      length = allocation->array_tot;
      data = allocation->array;
    }
    JSON values = JSON::array();
    for (int i = 0; i < (array ? length : 1); i++) {
      JSON value;
      switch (RNA_property_type(prop)) {
        case PROP_BOOLEAN:
          value = static_cast<bool *>(data)[i];
          break;
        case PROP_INT:
          value = static_cast<int *>(data)[i];
          break;
        case PROP_FLOAT:
          value = static_cast<float *>(data)[i];
          break;
        case PROP_ENUM:
          value = enum_json(C, &function_ptr, prop, *static_cast<int *>(data));
          break;
        case PROP_STRING: {
          const char *text = (flag & (PROP_DYNAMIC | PROP_THICK_WRAP)) ?
                                 static_cast<char *>(data) :
                                 *static_cast<char **>(data);
          value = text ? JSON(text) : JSON(nullptr);
          break;
        }
        case PROP_POINTER: {
          PointerRNA output;
          if (RNA_parameter_flag(prop) & PARM_RNAPTR) {
            PointerRNA *pointer = (flag & PROP_THICK_WRAP) ? static_cast<PointerRNA *>(data) :
                                                             *static_cast<PointerRNA **>(data);
            if (pointer)
              output = *pointer;
          }
          else {
            StructRNA *type = RNA_property_pointer_type(&function_ptr, prop);
            void *pointer = *static_cast<void **>(data);
            output = RNA_struct_is_ID(type) ?
                         RNA_id_pointer_create(static_cast<ID *>(pointer)) :
                         RNA_pointer_create_discrete(ptr.owner_id, type, pointer);
          }
          value = pointer_json(C, output);
          break;
        }
        case PROP_COLLECTION: {
          value = JSON::array();
          for (const PointerRNA &item : static_cast<CollectionVector *>(data)->items) {
            value.push_back(pointer_json(C, item));
          }
          break;
        }
      }
      values.push_back(value);
    }
    outputs[RNA_property_identifier(prop)] = array ? values : values[0];
  }
  RNA_parameter_list_end(&iter);
  return {{"value", outputs}, {"reports", reports.get()}};
}

static JSON object_execute(bContext *C, const JSON &request)
{
  const std::string action = request.at("action");
  if (action == "create") {
    const std::string type = request.value("type", "MESH");
    std::string op;
    JSON properties = JSON::object();
    if (type == "MESH") {
      const std::string primitive = request.value("primitive", "cube");
      require(primitive == "cube" || primitive == "plane" || primitive == "uv_sphere" ||
                  primitive == "cylinder",
              "Unknown mesh primitive: " + primitive);
      op = "MESH_OT_primitive_" + primitive + "_add";
    }
    else if (type == "ARMATURE")
      op = "OBJECT_OT_armature_add";
    else if (type == "EMPTY")
      op = "OBJECT_OT_empty_add";
    else if (type == "CAMERA")
      op = "OBJECT_OT_camera_add";
    else if (type == "LIGHT") {
      op = "OBJECT_OT_light_add";
      properties["type"] = "POINT";
    }
    else
      throw std::runtime_error("Unknown object type: " + type);
    for (const char *key : {"location", "rotation"}) {
      if (request.contains(key))
        properties[key] = request[key];
    }
    if (Object *active = CTX_data_active_object(C); active && active->mode != OB_MODE_OBJECT) {
      command_operator(C, "OBJECT_OT_mode_set", {{"mode", "OBJECT"}});
    }
    JSON result = command_operator(C, op, properties);
    Object *object = CTX_data_active_object(C);
    require(object != nullptr, "Creation did not produce an active object");
    PointerRNA ptr = RNA_id_pointer_create(&object->id);
    if (request.contains("name")) {
      command_set(C, {ptr, RNA_struct_find_property(&ptr, "name"), -1}, request["name"]);
    }
    /* Primitive operators can bake their scale into geometry; other add operators
     * ignore it. The object command consistently sets the object's RNA transform. */
    if (request.contains("scale")) {
      command_set(C, {ptr, RNA_struct_find_property(&ptr, "scale"), -1}, request["scale"]);
    }
    result["name"] = object->id.name + 2;
    return result;
  }
  JSON objects = request.value("objects", JSON::array());
  if (objects.empty() && request.contains("name"))
    objects.push_back(request["name"]);
  require(!objects.empty(), "Object action requires objects or name");
  if (action == "select") {
    command_select(C, objects, request.value("active", ""), request.value("mode", "OBJECT"));
    return {{"objects", objects}};
  }
  if (action == "delete") {
    command_select(C, objects);
    return command_operator(C, "OBJECT_OT_delete", {{"use_global", true}});
  }
  require(action == "transform", "Unknown object action: " + action);
  for (const JSON &name : objects) {
    Object *object = find_object(C, name);
    PointerRNA ptr = RNA_id_pointer_create(&object->id);
    if (request.contains("rotation")) {
      command_set(C, {ptr, RNA_struct_find_property(&ptr, "rotation_mode"), -1}, "XYZ");
    }
    for (const char *key : {"location", "rotation", "scale"}) {
      if (request.contains(key)) {
        const char *property = std::strcmp(key, "rotation") == 0 ? "rotation_euler" : key;
        command_set(C, {ptr, RNA_struct_find_property(&ptr, property), -1}, request[key]);
      }
    }
  }
  return {{"objects", objects}};
}

static JSON scene_execute(bContext *C, const JSON &request)
{
  const std::string action = request.at("action");
  if (action == "reset") {
    JSON result = command_operator(
        C, "WM_OT_read_factory_settings", {{"use_empty", request.value("empty", true)}});
    context_ensure(C);
    return result;
  }
  if (action == "frame") {
    Scene *scene = CTX_data_scene(C);
    PointerRNA ptr = RNA_id_pointer_create(&scene->id);
    command_set(
        C, {ptr, RNA_struct_find_property(&ptr, "frame_current"), -1}, request.at("frame"));
    BKE_scene_graph_update_for_newframe(CTX_data_ensure_evaluated_depsgraph(C));
    return {{"frame", scene->r.cfra}};
  }
  require(action == "save" || action == "open", "Unknown scene action: " + action);
  const std::string path = request.at("path");
  require(std::filesystem::path(path).is_absolute(), "Scene path must be absolute");
  JSON properties = {{"filepath", path}};
  if (action == "save")
    properties["check_existing"] = false;
  else
    properties["use_scripts"] = false;
  JSON result = command_operator(
      C, action == "save" ? "WM_OT_save_as_mainfile" : "WM_OT_open_mainfile", properties);
  context_ensure(C);
  result["path"] = path;
  if (action == "save") {
    result["external_effects"] = "Saved file is not restored by scene rollback";
  }
  return result;
}

CommandJSON command_execute(bContext *C, const JSON &request)
{
  require(BLI_thread_is_main(), "Native commands must execute on Blender's main thread");
  require(request.is_object(), "Native command must be an object");
  context_ensure(C);
  const std::string method = request.at("op");
  JSON result;
  if (method == "object")
    result = object_execute(C, request);
  else if (method == "data") {
    const std::string action = request.at("action");
    const std::string path = request.at("path");
    if (action == "call")
      result = data_call(C, path, request.value("arguments", JSON::object()));
    else {
      CommandProperty target = command_resolve(C, path);
      if (action == "set") {
        /* Resolve_property preserves a final pointer property for assignment. */
        PointerRNA root = RNA_main_pointer_create(CTX_data_main(C));
        require(RNA_path_resolve_property_full(
                    &root, path.c_str(), &target.pointer, &target.property, &target.index),
                "Assignment requires an RNA property: " + path);
        command_set(C, target, request.at("value"));
      }
      else
        require(action == "get", "Unknown data action: " + action);
      result = {{"value", command_get(C, target)}};
    }
  }
  else if (method == "operator") {
    const std::string action = request.value("action", "call");
    if (action == "list") {
      JSON names = JSON::array();
      for (wmOperatorType *type : WM_operatortypes_registered_get()) {
        if (!type->rna_ext.data)
          names.push_back(type->idname);
      }
      result = {{"operators", names}};
    }
    else if (action == "describe")
      result = operator_describe(C, request.at("name"));
    else {
      require(action == "call", "Unknown operator action: " + action);
      if (request.contains("objects"))
        command_select(
            C, request["objects"], request.value("active", ""), request.value("mode", "OBJECT"));
      else
        require(!request.contains("active") && !request.contains("mode"),
                "active/mode require explicit objects");
      result = command_operator(
          C, request.at("name"), request.value("properties", JSON::object()));
    }
  }
  else if (method == "scene")
    result = scene_execute(C, request);
  else if (method == "capabilities") {
    result = {{"native_commands",
               {"object",
                "data",
                "operator",
                "scene",
                "rig",
                "pose",
                "animation",
                "simulation",
                "render"}},
              {"features", JSON::object()}};
#define AGENT_FEATURE(name) result["features"][#name] = bool(AGENT_##name)
    AGENT_FEATURE(WITH_CYCLES);
    AGENT_FEATURE(WITH_BULLET);
    AGENT_FEATURE(WITH_MOD_FLUID);
    AGENT_FEATURE(WITH_MOD_OCEANSIM);
    AGENT_FEATURE(WITH_OPENVDB);
    AGENT_FEATURE(WITH_ALEMBIC);
    AGENT_FEATURE(WITH_USD);
    AGENT_FEATURE(WITH_IO_FBX);
    AGENT_FEATURE(WITH_IO_WAVEFRONT_OBJ);
    AGENT_FEATURE(WITH_IO_PLY);
    AGENT_FEATURE(WITH_IO_STL);
    AGENT_FEATURE(WITH_VULKAN_BACKEND);
    AGENT_FEATURE(WITH_METAL_BACKEND);
    AGENT_FEATURE(WITH_CYCLES_OSL);
    AGENT_FEATURE(WITH_AUDASPACE);
    AGENT_FEATURE(WITH_MATERIALX);
    AGENT_FEATURE(WITH_HYDRA);
    AGENT_FEATURE(WITH_LIBMV);
    AGENT_FEATURE(WITH_FREESTYLE);
    AGENT_FEATURE(WITH_IO_GREASE_PENCIL);
    AGENT_FEATURE(WITH_HARU);
    AGENT_FEATURE(WITH_IMAGE_CINEON);
    AGENT_FEATURE(WITH_IMAGE_OPENJPEG);
    AGENT_FEATURE(WITH_CODEC_FFMPEG);
    AGENT_FEATURE(WITH_CODEC_SNDFILE);
#undef AGENT_FEATURE
  }
  else if (method == "rig" || method == "pose" || method == "animation" ||
           method == "simulation" || method == "render")
  {
    result = production_execute(C, request);
  }
  else
    throw std::runtime_error("Unknown native command: " + method);
  ED_editors_flush_edits(CTX_data_main(C));
  BKE_scene_graph_update_tagged(CTX_data_ensure_evaluated_depsgraph(C), CTX_data_main(C));
  return result;
}

static PyObject *command_python(PyObject *self, PyObject *argument)
{
  const char *text = PyUnicode_AsUTF8(argument);
  if (!text)
    return nullptr;
  bContext *C = static_cast<bContext *>(PyCapsule_GetPointer(self, "agent.context"));
  if (!C)
    return nullptr;
  try {
    const std::string result = command_execute(C, JSON::parse(text)).dump();
    return PyUnicode_FromStringAndSize(result.data(), result.size());
  }
  catch (const std::exception &error) {
    PyErr_SetString(PyExc_RuntimeError, error.what());
    return nullptr;
  }
}

PyObject *command_api(bContext *C)
{
  static PyMethodDef method = {"command", command_python, METH_O, nullptr};
  PyObject *capsule = PyCapsule_New(C, "agent.context", nullptr);
  PyObject *function = PyCFunction_New(&method, capsule);
  Py_DECREF(capsule);
  return function;
}

}  // namespace blender::agent
