/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "agent_production.hh"
#include "agent_session.hh"

#include <cmath>
#include <memory>
#include <stdexcept>
#include <vector>

#include "ANIM_keyframing.hh"
#include "ANIM_visualkey.hh"
#include "BKE_animsys.hh"
#include "BKE_armature.hh"
#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_image_format.hh"
#include "BKE_lib_id.hh"
#include "BKE_main.hh"
#include "BKE_modifier.hh"
#include "BKE_ocean.h"
#include "BKE_pointcache.h"
#include "BKE_scene.hh"
#include "BLI_fileops.hh"
#include "BLI_listbase.hh"
#include "BLI_path_utils.hh"
#include "BLI_string.hh"
#include "DEG_depsgraph.hh"
#include "DEG_depsgraph_query.hh"
#include "DNA_action_types.h"
#include "DNA_armature_types.h"
#include "DNA_constraint_types.h"
#include "DNA_modifier_types.h"
#include "DNA_object_force_types.h"
#include "DNA_object_types.h"
#include "DNA_rigidbody_types.h"
#include "DNA_scene_types.h"
#include "ED_armature.hh"
#include "RNA_access.hh"
#include "RNA_enum_types.hh"
#include "RNA_path.hh"
#include "RNA_prototypes.hh"

namespace blender::agent {

static std::string object_path(const std::string &name)
{
  return "objects[" + CommandJSON(name).dump() + "]";
}

static Object *object_find(bContext *C, const std::string &name)
{
  for (Object &object : CTX_data_main(C)->objects) {
    if (name == object.id.name + 2) {
      return &object;
    }
  }
  throw std::runtime_error("Object not found: " + name);
}

static void set(bContext *C, const std::string &path, const CommandJSON &value)
{
  command_set(C, command_resolve(C, path), value);
}

static void frame_set(bContext *C, float frame)
{
  if (!std::isfinite(frame) || frame < MINAFRAMEF || frame > MAXFRAMEF) {
    throw std::invalid_argument("Frame is outside Blender's supported range");
  }
  BKE_scene_frame_set(CTX_data_scene(C), frame);
  BKE_scene_graph_update_for_newframe(CTX_data_depsgraph_pointer(C));
}

static void check_cancel()
{
  if (request_cancelled()) {
    throw std::runtime_error("Production operation cancelled; external files may be partial");
  }
}

static void triple(const CommandJSON &value, float result[3])
{
  if (!value.is_array() || value.size() != 3) {
    throw std::invalid_argument("Expected a three-component vector");
  }
  for (int i = 0; i < 3; i++) {
    result[i] = value.at(i).get<float>();
    if (!std::isfinite(result[i])) {
      throw std::invalid_argument("Vector components must be finite");
    }
  }
}

static CommandJSON rig_execute(bContext *C, const CommandJSON &request)
{
  const std::string action = request.at("action"), name = request.at("name");
  if (action == "create") {
    command_operator(C, "object.add", {{"type", "ARMATURE"}});
    Object *object = CTX_data_active_object(C);
    BKE_id_rename(*CTX_data_main(C), object->id, name);
    return {{"name", object->id.name + 2}};
  }
  Object *object = object_find(C, name);
  if (object->type != OB_ARMATURE) {
    throw std::invalid_argument("Rig must be an armature");
  }
  if (action == "bone") {
    float head[3], tail[3];
    triple(request.at("head"), head);
    triple(request.at("tail"), tail);
    if (head[0] == tail[0] && head[1] == tail[1] && head[2] == tail[2]) {
      throw std::invalid_argument("Bone head and tail must differ");
    }
    const std::string bone_name = request.at("bone");
    command_select(C, {name}, name, "EDIT");
    auto *armature = id_cast<bArmature *>(object->data);
    EditBone *parent = nullptr;
    if (request.contains("parent")) {
      const std::string parent_name = request.at("parent");
      parent = ED_armature_ebone_find_name(armature->edbo, parent_name.c_str());
      if (!parent) {
        command_select(C, {name}, name);
        throw std::invalid_argument("Parent bone not found: " + parent_name);
      }
    }
    EditBone *bone = ED_armature_ebone_add(armature, bone_name.c_str());
    for (int i = 0; i < 3; i++) {
      bone->head[i] = head[i];
      bone->tail[i] = tail[i];
    }
    bone->parent = parent;
    const std::string actual_name = bone->name;
    command_select(C, {name}, name);
    DEG_id_tag_update(&object->id, ID_RECALC_GEOMETRY);
    return {{"name", name}, {"bone", actual_name}};
  }
  if (action == "bind") {
    CommandJSON objects = request.at("objects");
    if (!objects.is_array() || objects.empty()) {
      throw std::invalid_argument("Binding requires mesh objects");
    }
    for (const std::string mesh_name : objects) {
      if (object_find(C, mesh_name)->type != OB_MESH) {
        throw std::invalid_argument("Binding target must be a mesh: " + mesh_name);
      }
    }
    const std::string weights = request.value("weights", "automatic");
    std::string type;
    if (weights == "automatic") {
      type = "ARMATURE_AUTO";
    }
    else if (weights == "envelope") {
      type = "ARMATURE_ENVELOPE";
    }
    else if (weights == "empty") {
      type = "ARMATURE_NAME";
    }
    else {
      throw std::invalid_argument("weights must be automatic, envelope, or empty");
    }
    objects.push_back(name);
    command_select(C, objects, name);
    return command_operator(C, "object.parent_set", {{"type", type}});
  }
  throw std::invalid_argument("Unknown rig action: " + action);
}

static CommandJSON pose_execute(bContext *C, const CommandJSON &request)
{
  if (request.at("action") != "set") {
    throw std::invalid_argument("Unknown pose action");
  }
  const std::string name = request.at("name"), bone = request.at("bone");
  const std::string path = object_path(name) + ".pose.bones[" + CommandJSON(bone).dump() + "]";
  command_resolve(C, path + ".location");
  if (!request.contains("location") && !request.contains("rotation") && !request.contains("scale"))
  {
    throw std::invalid_argument("Pose set requires location, rotation, or scale");
  }
  for (const char *field : {"location", "rotation", "scale"}) {
    if (request.contains(field)) {
      float values[3];
      triple(request.at(field), values);
      if (std::string(field) == "rotation") {
        set(C, path + ".rotation_mode", "XYZ");
        set(C, path + ".rotation_euler", request.at(field));
      }
      else {
        set(C, path + "." + field, request.at(field));
      }
    }
  }
  return {{"name", name}, {"bone", bone}};
}

static int keyframe(bContext *C, const std::string &path, int index, float frame, bool remove)
{
  if (!std::isfinite(frame) || frame < MINAFRAMEF || frame > MAXFRAMEF) {
    throw std::invalid_argument("Keyframe is outside Blender's supported frame range");
  }
  if (index < -1) {
    throw std::invalid_argument("Keyframe index must be -1 or a valid array index");
  }
  CommandProperty target = command_resolve(C, path);
  if (!target.property || !RNA_property_animateable(&target.pointer, target.property)) {
    throw std::invalid_argument("Animation path must address an animatable property");
  }
  const auto relative = RNA_path_from_ID_to_property(&target.pointer, target.property);
  if (!relative || !target.pointer.owner_id) {
    throw std::invalid_argument("Animation path must address a property owned by an ID");
  }
  RNAPath rna_path{*relative};
  index = index == -1 ? target.index : index;
  const int length = RNA_property_array_length(&target.pointer, target.property);
  if (index >= 0 && (length == 0 || index >= length)) {
    throw std::invalid_argument("Keyframe array index is outside the property range");
  }
  if (index >= 0) {
    rna_path.index = index;
  }
  if (remove) {
    return animrig::delete_keyframe(
        CTX_data_main(C), nullptr, target.pointer.owner_id, rna_path, frame);
  }
  PointerRNA owner = RNA_id_pointer_create(target.pointer.owner_id);
  const AnimationEvalContext eval = BKE_animsys_eval_context_construct(
      CTX_data_depsgraph_pointer(C), frame);
  const animrig::CombinedKeyingResult result = animrig::insert_keyframes(CTX_data_main(C),
                                                                         &owner,
                                                                         std::nullopt,
                                                                         {rna_path},
                                                                         frame,
                                                                         eval,
                                                                         BEZT_KEYTYPE_KEYFRAME,
                                                                         eInsertKeyFlags(0));
  if (result.has_errors()) {
    throw std::runtime_error("Keyframe insertion failed for " + path);
  }
  return result.get_count(animrig::SingleKeyingResult::SUCCESS);
}

static CommandJSON animation_execute(bContext *C, const CommandJSON &request)
{
  const std::string action = request.at("action");
  if (action == "key" || action == "delete") {
    return {{"keyframes",
             keyframe(C,
                      request.at("path"),
                      request.value("index", -1),
                      request.value("frame", float(CTX_data_scene(C)->r.cfra)),
                      action == "delete")}};
  }
  if (action != "bake") {
    throw std::invalid_argument("Unknown animation action: " + action);
  }
  const int start = request.at("start"), end = request.at("end");
  const int step = request.value("step", 1);
  if (step < 1 || end < start) {
    throw std::invalid_argument("Bake requires start <= end and positive step");
  }
  std::vector<std::string> paths;
  if (request.contains("path")) {
    paths.push_back(request.at("path"));
  }
  else {
    for (const std::string name : request.at("objects")) {
      Object *object = object_find(C, name);
      std::vector<std::string> bases{object_path(name)};
      if (object->pose) {
        for (bPoseChannel &bone : object->pose->chanbase) {
          bases.push_back(object_path(name) + ".pose.bones[" + CommandJSON(bone.name).dump() +
                          "]");
        }
      }
      for (const std::string &base : bases) {
        const auto mode = command_get(C, command_resolve(C, base + ".rotation_mode"));
        paths.push_back(base + ".location");
        paths.push_back(base + ".scale");
        paths.push_back(base + (mode == "QUATERNION" ? ".rotation_quaternion" :
                                mode == "AXIS_ANGLE" ? ".rotation_axis_angle" :
                                                       ".rotation_euler"));
      }
    }
  }
  if (paths.empty()) {
    throw std::invalid_argument("Bake requires a path or nonempty objects");
  }
  /* Sample the whole range before writing keys: inserted keys must not influence
   * the subsequent samples. This is native visual-keying, including constraints. */
  struct Sample {
    int frame;
    std::string path;
    CommandJSON value;
  };
  std::vector<Sample> samples;
  const float original_frame = BKE_scene_frame_get(CTX_data_scene(C));
  try {
    for (int64_t frame = start; frame <= end; frame += step) {
      check_cancel();
      frame_set(C, frame);
      for (const std::string &path : paths) {
        CommandProperty target = command_resolve(C, path);
        PointerRNA evaluated = target.pointer;
        if (target.pointer.owner_id) {
          const auto relative = RNA_path_from_ID_to_struct(&target.pointer);
          PointerRNA owner = RNA_id_pointer_create(
              DEG_get_evaluated_id(CTX_data_depsgraph_pointer(C), target.pointer.owner_id));
          if (relative && !relative->empty()) {
            if (!RNA_path_resolve(&owner, relative->c_str(), &evaluated, nullptr)) {
              throw std::runtime_error("Cannot resolve evaluated bake path: " + path);
            }
          }
          else {
            evaluated = owner;
          }
        }
        CommandJSON value;
        if (!request.contains("path") &&
            (evaluated.type == RNA_Object || evaluated.type == RNA_PoseBone))
        {
          const Vector<float> values = animrig::visualkey_get_values(&evaluated, target.property);
          value = CommandJSON::array();
          for (float item : values) {
            value.push_back(item);
          }
        }
        else {
          value = command_get(C, {evaluated, target.property, target.index});
        }
        samples.push_back({int(frame), path, std::move(value)});
      }
      request_progress("animation sample",
                       end == start ? 1.0f : float(frame - start) / float(end - start));
    }
    int count = 0;
    size_t written = 0;
    for (const Sample &sample : samples) {
      check_cancel();
      set(C, sample.path, sample.value);
      count += keyframe(C, sample.path, request.value("index", -1), sample.frame, false);
      request_progress("animation key", float(++written) / float(samples.size()));
    }
    if (!request.contains("path")) {
      /* Object visual keys are world-space. Remove the parent contribution and
       * delta transforms; mute the solved constraints instead of applying twice.
       * Keep the constraints themselves available for subsequent editing. */
      for (const std::string name : request.at("objects")) {
        Object *object = object_find(C, name);
        const std::string base = object_path(name);
        set(C, base + ".parent", nullptr);
        set(C, base + ".delta_location", {0.0, 0.0, 0.0});
        set(C, base + ".delta_rotation_euler", {0.0, 0.0, 0.0});
        set(C, base + ".delta_rotation_quaternion", {1.0, 0.0, 0.0, 0.0});
        set(C, base + ".delta_scale", {1.0, 1.0, 1.0});
        for (bConstraint &constraint : object->constraints) {
          set(C, base + ".constraints[" + CommandJSON(constraint.name).dump() + "].mute", true);
        }
        if (object->pose) {
          for (bPoseChannel &bone : object->pose->chanbase) {
            for (bConstraint &constraint : bone.constraints) {
              set(C,
                  base + ".pose.bones[" + CommandJSON(bone.name).dump() + "].constraints[" +
                      CommandJSON(constraint.name).dump() + "].mute",
                  true);
            }
          }
        }
        if (object->rigidbody_object) {
          set(C, base + ".rigid_body.kinematic", true);
        }
      }
    }
    frame_set(C, original_frame);
    return {{"keyframes", count},
            {"bake_effects",
             request.contains("path") ? "Channel sampled; dependencies retained" :
                                        "World transforms keyed; parents cleared, constraints "
                                        "muted, rigid bodies kinematic"}};
  }
  catch (...) {
    frame_set(C, original_frame);
    throw;
  }
}

static void cache_progress(void *, float progress, int *cancel)
{
  *cancel = request_cancelled();
  request_progress("simulation", std::isfinite(progress) ? progress : 1.0f);
}

static void ocean_bake(bContext *C, Object *object, OceanModifierData *modifier)
{
  /* OBJECT_OT_ocean_bake starts a window-manager job even from EXEC. A request
   * must instead finish its bake before feedback/snapshotting can inspect it. */
  const char *relbase = BKE_modifier_path_relbase(CTX_data_main(C), object);
  char directory[FILE_MAX];
  STRNCPY(directory, modifier->cachepath);
  BLI_path_abs(directory, relbase);
  BLI_path_abs_from_cwd(directory, sizeof(directory));
  if (!BLI_dir_create_recursive(directory)) {
    throw std::runtime_error("Cannot create ocean cache directory: " + std::string(directory));
  }
  using Cache = std::unique_ptr<OceanCache, decltype(&BKE_ocean_free_cache)>;
  using Simulation = std::unique_ptr<Ocean, decltype(&BKE_ocean_free)>;
  Cache cache(BKE_ocean_init_cache(modifier->cachepath,
                                   relbase,
                                   modifier->bakestart,
                                   modifier->bakeend,
                                   modifier->wave_scale,
                                   modifier->chop_amount,
                                   modifier->foam_coverage,
                                   modifier->foam_fade,
                                   modifier->resolution),
              BKE_ocean_free_cache);
  if (!cache) {
    throw std::runtime_error("Ocean simulation is unavailable in this build");
  }
  cache->time = MEM_new_array_uninitialized<float>(cache->duration, "agent ocean bake time");
  const float original_time = modifier->time;
  for (int frame = cache->start; frame <= cache->end; frame++) {
    const AnimationEvalContext eval = BKE_animsys_eval_context_construct(
        CTX_data_depsgraph_pointer(C), frame);
    BKE_animsys_evaluate_animdata(&object->id, object->adt, &eval, ADT_RECALC_ANIM, false);
    cache->time[frame - cache->start] = modifier->time;
  }
  modifier->time = original_time;
  Simulation ocean(BKE_ocean_add(), BKE_ocean_free);
  if (!ocean || !BKE_ocean_init_from_modifier(ocean.get(), modifier, modifier->resolution)) {
    throw std::runtime_error("Cannot initialize ocean simulation");
  }
  check_cancel();
  BKE_ocean_bake(ocean.get(), cache.get(), cache_progress, nullptr);
  check_cancel();
  if (!cache->baked) {
    throw std::runtime_error("Ocean simulation did not complete");
  }
  /* The kernel logs write failures but has no failure return. Read its own cache
   * back before publishing 'cached', including each enabled output channel. */
  for (int frame = cache->start; frame <= cache->end; frame++) {
    check_cancel();
    BKE_ocean_simulate_cache(cache.get(), frame);
    const int i = frame - cache->start;
    if (!cache->ibufs_disp[i] ||
        ((modifier->flag & MOD_OCEAN_GENERATE_FOAM) && !cache->ibufs_foam[i]) ||
        ((modifier->flag & MOD_OCEAN_GENERATE_NORMALS) && !cache->ibufs_norm[i]) ||
        ((modifier->flag & MOD_OCEAN_GENERATE_FOAM) &&
         (modifier->flag & MOD_OCEAN_GENERATE_SPRAY) &&
         (!cache->ibufs_spray[i] || !cache->ibufs_spray_inverse[i])))
    {
      throw std::runtime_error("Ocean cache output is missing or unreadable at frame " +
                               std::to_string(frame));
    }
  }
  BKE_ocean_free_modifier_cache(modifier);
  modifier->oceancache = cache.release();
  modifier->cached = true;
  DEG_id_tag_update(&object->id, ID_RECALC_SYNC_TO_EVAL);
  request_progress("simulation", 1.0f);
}

static CommandJSON simulation_execute(bContext *C, const CommandJSON &request)
{
  const std::string name = request.at("name"), action = request.at("action");
  const std::string type = request.at("type");
  Object *object = object_find(C, name);
  command_select(C, {name}, name);
  ModifierType modifier_type;
  if (type == "CLOTH") {
    modifier_type = eModifierType_Cloth;
  }
  else if (type == "SOFT_BODY") {
    modifier_type = eModifierType_Softbody;
  }
  else if (type == "FLUID") {
    modifier_type = eModifierType_Fluid;
  }
  else if (type == "OCEAN") {
    modifier_type = eModifierType_Ocean;
  }
  else if (type == "RIGID_BODY") {
    modifier_type = eModifierType_None;
  }
  else {
    throw std::invalid_argument("Unknown simulation type: " + type);
  }
  if (action != "add" && action != "bake" && action != "free") {
    throw std::invalid_argument("Unknown simulation action: " + action);
  }
  if (action == "add") {
    if (type == "RIGID_BODY") {
      command_operator(C, "rigidbody.object_add", {{"type", "ACTIVE"}});
    }
    else {
      if (BKE_modifiers_findby_type(object, modifier_type)) {
        throw std::invalid_argument("Object already has this simulation modifier");
      }
      command_operator(C, "object.modifier_add", {{"type", type}});
    }
  }
  ModifierData *modifier = modifier_type == eModifierType_None ?
                               nullptr :
                               BKE_modifiers_findby_type(object, modifier_type);
  if (modifier_type != eModifierType_None && !modifier) {
    throw std::invalid_argument("Object has no " + type + " modifier");
  }
  const std::string base = modifier ? object_path(name) + ".modifiers[" +
                                          CommandJSON(modifier->name).dump() + "]" :
                                      "";
  const int start = request.value("start", CTX_data_scene(C)->r.sfra);
  const int end = request.value("end", CTX_data_scene(C)->r.efra);
  if (end < start) {
    throw std::invalid_argument("Simulation requires start <= end");
  }
  if (type == "FLUID" || type == "OCEAN") {
    if (type == "FLUID" && action == "add") {
      set(C, base + ".fluid_type", "DOMAIN");
    }
    const std::string start_path = base + (type == "FLUID" ? ".domain_settings.cache_frame_start" :
                                                             ".frame_start");
    const std::string end_path = base + (type == "FLUID" ? ".domain_settings.cache_frame_end" :
                                                           ".frame_end");
    if (action != "free") {
      if (action == "add" || request.contains("start")) {
        set(C, start_path, start);
      }
      if (action == "add" || request.contains("end")) {
        set(C, end_path, end);
      }
      if (type == "FLUID") {
        set(C, base + ".domain_settings.cache_type", "ALL");
      }
      if (request.contains("path")) {
        set(C,
            base + (type == "FLUID" ? ".domain_settings.cache_directory" : ".filepath"),
            request.at("path"));
      }
      if (command_get(C, command_resolve(C, start_path)).get<int>() >
          command_get(C, command_resolve(C, end_path)).get<int>())
      {
        throw std::invalid_argument("Simulation requires start <= end");
      }
    }
    const std::string cache_path = command_get(
        C,
        command_resolve(
            C, base + (type == "FLUID" ? ".domain_settings.cache_directory" : ".filepath")));
    if (action != "add") {
      check_cancel();
      if (type == "FLUID") {
        command_operator(C, action == "bake" ? "fluid.bake_all" : "fluid.free_all");
      }
      else if (action == "bake") {
        ocean_bake(C, object, reinterpret_cast<OceanModifierData *>(modifier));
      }
      else {
        command_operator(
            C, "object.ocean_bake", {{"modifier", modifier->name}, {"free", action == "free"}});
      }
      check_cancel();
      if (action == "bake") {
        const std::string status_path = base + (type == "FLUID" ?
                                                    ".domain_settings.has_cache_baked_data" :
                                                    ".is_cached");
        if (!command_get(C, command_resolve(C, status_path)).get<bool>()) {
          throw std::runtime_error("Simulation did not produce a baked cache");
        }
      }
    }
    return {{"modifier", modifier->name},
            {"cache_path", cache_path},
            {"external_effects",
             action == "add" ?
                 "none" :
                 "Cache files may be written or removed; scene rollback does not restore files"}};
  }
  PTCacheID cache{};
  if (type == "RIGID_BODY") {
    RigidBodyWorld *world = CTX_data_scene(C)->rigidbody_world;
    if (!object->rigidbody_object || !world) {
      throw std::invalid_argument("Object has no rigid body world");
    }
    BKE_ptcache_id_from_rigidbody(&cache, object, world);
  }
  else if (type == "CLOTH") {
    BKE_ptcache_id_from_cloth(&cache, object, reinterpret_cast<ClothModifierData *>(modifier));
  }
  else {
    BKE_ptcache_id_from_softbody(&cache, object, object->soft);
  }
  if (!cache.cache) {
    throw std::runtime_error("Simulation has no point cache");
  }
  if (action != "free" && (cache.cache->flag & PTCACHE_BAKED)) {
    throw std::invalid_argument("Free the existing bake before changing its range or rebaking");
  }
  if (action != "free") {
    if (action == "add" || request.contains("start")) {
      cache.cache->startframe = start;
    }
    if (action == "add" || request.contains("end")) {
      cache.cache->endframe = end;
    }
    if (cache.cache->endframe < cache.cache->startframe) {
      throw std::invalid_argument("Simulation requires start <= end");
    }
  }
  if (action == "bake") {
    PTCacheBaker baker{};
    baker.bmain = CTX_data_main(C);
    baker.scene = CTX_data_scene(C);
    baker.view_layer = CTX_data_view_layer(C);
    baker.depsgraph = CTX_data_depsgraph_pointer(C);
    baker.bake = true;
    baker.quick_step = 1;
    baker.pid = cache;
    baker.update_progress = cache_progress;
    check_cancel();
    BKE_ptcache_bake(&baker);
    check_cancel();
    if (!(cache.cache->flag & PTCACHE_BAKED)) {
      throw std::runtime_error("Simulation did not produce a baked cache");
    }
  }
  else if (action == "free") {
    cache.cache->flag &= ~PTCACHE_BAKED;
    BKE_ptcache_id_clear(&cache, PTCACHE_CLEAR_ALL, 0);
  }
  if (action != "bake") {
    DEG_id_tag_update(&object->id, ID_RECALC_GEOMETRY);
  }
  return {
      {"baked", bool(cache.cache->flag & PTCACHE_BAKED)},
      {"cache_scope", type == "RIGID_BODY" ? "scene rigid body world" : "object"},
      {"external_effects", "Disk cache files, if enabled, are not restored by scene rollback"}};
}

static CommandJSON render_execute(bContext *C, const CommandJSON &request)
{
  const std::string action = request.at("action");
  if (action != "frame" && action != "animation") {
    throw std::invalid_argument("Unknown render action: " + action);
  }
  Scene *scene = CTX_data_scene(C);
  if (!scene->camera) {
    throw std::invalid_argument("Production rendering requires a scene camera");
  }
  const std::string base = "scenes[" + CommandJSON(scene->id.name + 2).dump() + "]";
  if (request.contains("format")) {
    const std::string format = request.at("format");
    int type;
    if (!RNA_enum_value_from_id(rna_enum_image_type_all_items, format.c_str(), &type)) {
      throw std::invalid_argument("Unknown render format: " + format);
    }
    /* File-format RNA is filtered by the current media type. The kernel setter
     * switches both together, including image -> movie and movie -> image. */
    BKE_image_format_set(&scene->r.im_format, &scene->id, type);
  }
  for (const auto &[field, property] : {std::pair{"path", "render.filepath"},
                                        {"engine", "render.engine"},
                                        {"width", "render.resolution_x"},
                                        {"height", "render.resolution_y"},
                                        {"start", "frame_start"},
                                        {"end", "frame_end"}})
  {
    if (request.contains(field)) {
      set(C, base + "." + property, request.at(field));
    }
  }
  if (scene->r.efra < scene->r.sfra) {
    throw std::invalid_argument("Render requires start <= end");
  }
  if (request.contains("width") || request.contains("height")) {
    set(C, base + ".render.resolution_percentage", 100);
  }
  if (request.contains("samples")) {
    const std::string engine = command_get(C, command_resolve(C, base + ".render.engine"));
    if (engine == "CYCLES") {
      set(C, base + ".cycles.samples", request.at("samples"));
    }
    else if (engine == "BLENDER_EEVEE") {
      set(C, base + ".eevee.taa_render_samples", request.at("samples"));
    }
    else {
      throw std::invalid_argument("samples is supported by Cycles and EEVEE only");
    }
  }
  if (request.contains("frame")) {
    frame_set(C, request.at("frame").get<float>());
  }
  check_cancel();
  command_operator(C,
                   "render.render",
                   {{"animation", action == "animation"}, {"write_still", action == "frame"}});
  check_cancel();
  return {{"path", command_get(C, command_resolve(C, base + ".render.filepath"))},
          {"external_effects", "Render output files are not restored by scene rollback"}};
}

CommandJSON production_execute(bContext *C, const CommandJSON &request)
{
  const std::string op = request.at("op");
  if (op == "rig") {
    return rig_execute(C, request);
  }
  if (op == "pose") {
    return pose_execute(C, request);
  }
  if (op == "animation") {
    return animation_execute(C, request);
  }
  if (op == "simulation") {
    return simulation_execute(C, request);
  }
  if (op == "render") {
    return render_execute(C, request);
  }
  throw std::invalid_argument("Unknown production command: " + op);
}

}  // namespace blender::agent
