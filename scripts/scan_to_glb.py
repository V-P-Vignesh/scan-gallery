"""
scan_to_glb.py — high-poly scan mesh -> low-poly GLB with baked normal + colour maps.

Run headless (no GUI):
    blender -b -P scan_to_glb.py -- --input statue_highpoly.ply --output statue.glb --faces 50000

Batch a folder (bash):
    for f in meshes/*.ply; do
        blender -b -P scan_to_glb.py -- --input "$f" --output "web/models/$(basename ${f%.ply}).glb"
    done

WHAT IT DOES AND WHY
--------------------
A Poisson mesh straight out of CloudCompare is typically 2-10 million triangles with
per-vertex colour. That is unusable in a browser: a 300 MB file that stalls a phone.

The fix is the standard games/VFX trick of "baking". We keep two versions of the model:

  * HIGH poly  — the original scan mesh. Carries all the real geometric detail
                 (chisel marks, drapery folds) and the scanner's RGB in vertex colours.
  * LOW poly   — a decimated copy, ~1-2% of the triangles. Cheap to render, but smooth
                 and featureless on its own.

We then project the high-poly surface onto the low-poly one and record, per texel:
  * the NORMAL map  — the direction the real surface faces at that point. The GPU uses
                      this when shading, so light behaves as if the detail were still
                      there. Detail survives as a 2D image instead of as triangles.
  * the DIFFUSE map — the scanner's vertex colours resampled into a texture.

Result: ~1% of the file size, visually near-identical under lighting. This is exactly
how heritage models are published on Sketchfab, Europeana and museum portals — so it is
a defensible, documentable deliverable step, not a cosmetic shortcut.

Real-world scale is preserved (no normalisation), so the GLB is metrically correct and
model-viewer's AR mode will place it at true size. That matters for documentation value.

Tested on Blender 4.x. Needs Cycles for baking (Blender ships with it).
"""

import bpy
import bmesh
import sys
import os
import argparse
from pathlib import Path
from mathutils import Vector


# ----------------------------------------------------------------------------- args
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="high-poly mesh (.ply/.obj/.stl/.glb)")
    p.add_argument("--output", required=True, help="output .glb path")
    p.add_argument("--faces", type=int, default=50000, help="target triangle count for the low-poly")
    p.add_argument("--texsize", type=int, default=2048, help="baked texture resolution (2048 is a good default)")
    p.add_argument("--no-color", action="store_true", help="skip the colour bake (geometry-only scans)")
    p.add_argument("--keep-blend", default="", help="optional path to also save the .blend for inspection")
    return p.parse_args(argv)


# ------------------------------------------------------------------------- helpers
def clear_scene():
    """Start from an empty file: -b still loads the default cube/camera/light."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_mesh(path):
    ext = Path(path).suffix.lower()
    before = set(bpy.data.objects)

    if ext == ".ply":
        # Blender 4.x renamed the PLY importer; fall back for 3.x
        if hasattr(bpy.ops.wm, "ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == ".stl":
        if hasattr(bpy.ops.wm, "stl_import"):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise RuntimeError(f"Unsupported input format: {ext}")

    new = [o for o in set(bpy.data.objects) - before if o.type == "MESH"]
    if not new:
        raise RuntimeError("No mesh found in the imported file.")

    # Some exporters split the mesh; join everything into one object.
    bpy.ops.object.select_all(action="DESELECT")
    for o in new:
        o.select_set(True)
    bpy.context.view_layer.objects.active = new[0]
    if len(new) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def center_on_origin(obj):
    """
    Move the object so its base sits on Z=0 and it is centred in X/Y.
    WHY: model-viewer orbits around the origin and AR places the origin on the floor.
    An object sitting 400 m from origin (common after TLS georeferencing) will appear
    off-screen or float in AR. Scale is NOT touched — metric accuracy is preserved.
    """
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bb = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_x = min(v.x for v in bb); max_x = max(v.x for v in bb)
    min_y = min(v.y for v in bb); max_y = max(v.y for v in bb)
    min_z = min(v.z for v in bb)
    obj.location.x -= (min_x + max_x) / 2
    obj.location.y -= (min_y + max_y) / 2
    obj.location.z -= min_z
    bpy.ops.object.transform_apply(location=True)


def bbox_diagonal(obj):
    bb = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    return (max(bb, key=lambda v: v.length) - min(bb, key=lambda v: v.length)).length


def triangulate(obj):
    """Decimate ratios and bakes behave predictably only on triangles."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    me.update()


def vertex_colour_layer(obj):
    """Find the scanner RGB. CloudCompare/FARO PLYs name it 'Col', 'Color' or similar."""
    me = obj.data
    if hasattr(me, "color_attributes") and len(me.color_attributes):
        return me.color_attributes[0].name
    if hasattr(me, "vertex_colors") and len(me.vertex_colors):
        return me.vertex_colors[0].name
    return None


def make_highpoly_material(obj, colour_attr):
    """
    The bake reads the SOURCE material, so the high-poly needs its vertex colours
    wired into Base Color — otherwise the diffuse bake comes out flat grey.
    """
    mat = bpy.data.materials.new("HI_src")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    if colour_attr:
        ca = nt.nodes.new("ShaderNodeVertexColor")
        ca.layer_name = colour_attr
        ca.location = (-300, 0)
        nt.links.new(ca.outputs["Color"], bsdf.inputs["Base Color"])
    obj.data.materials.clear()
    obj.data.materials.append(mat)


def unwrap(obj):
    """
    Smart UV Project: auto-cuts the low-poly into flat islands so a 2D texture can be
    painted onto it. Required before any bake — without UVs there is nowhere to bake to.
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.003)  # 1.15 rad = 66 deg
    except Exception as e:
        print(f"[warn] smart_project failed ({e}); falling back to cube projection")
        bpy.ops.uv.cube_project(cube_size=1.0)
    bpy.ops.object.mode_set(mode="OBJECT")


def new_image(name, size, is_data=False):
    img = bpy.data.images.new(name, width=size, height=size, alpha=False,
                              float_buffer=False, is_data=is_data)
    if is_data:
        img.colorspace_settings.name = "Non-Color"
    return img


def bake_to_image(low, high, img, bake_type, extrusion):
    """
    'Selected to active': select the high-poly, make the low-poly active, and Cycles
    shoots a ray from each low-poly texel outwards to find the high-poly surface and
    records what it finds there. cage_extrusion is how far out the rays start; too small
    and concave areas miss, too large and nearby surfaces bleed into each other.
    """
    nt = low.data.materials[0].node_tree
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = img
    nt.nodes.active = node          # the ACTIVE image node is the bake target
    node.select = True

    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low   # active = destination

    scene = bpy.context.scene
    scene.render.bake.use_selected_to_active = True
    scene.render.bake.cage_extrusion = extrusion
    scene.render.bake.max_ray_distance = extrusion * 2
    scene.render.bake.margin = 8      # bleed past island edges, stops seams at low mip levels

    if bake_type == "NORMAL":
        scene.render.bake.normal_space = "TANGENT"
        bpy.ops.object.bake(type="NORMAL")
    else:
        # colour only — no lighting baked in, so the model relights correctly in any viewer
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
        scene.render.bake.use_pass_color = True
        bpy.ops.object.bake(type="DIFFUSE")

    return node


# ----------------------------------------------------------------------------- main
def main():
    args = parse_args()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    clear_scene()

    print(f"[1/7] importing {args.input}")
    high = import_mesh(args.input)
    high.name = "HIGH"
    triangulate(high)
    center_on_origin(high)
    tri_in = len(high.data.polygons)
    diag = bbox_diagonal(high)
    print(f"      {tri_in:,} triangles, bbox diagonal {diag:.2f} m")

    colour_attr = vertex_colour_layer(high)
    print(f"[2/7] vertex colour layer: {colour_attr or 'NONE (geometry only)'}")
    make_highpoly_material(high, colour_attr)

    print(f"[3/7] decimating to ~{args.faces:,} triangles")
    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    bpy.context.view_layer.objects.active = high
    bpy.ops.object.duplicate()
    low = bpy.context.view_layer.objects.active
    low.name = "LOW"

    ratio = min(1.0, args.faces / max(1, tri_in))
    dec = low.modifiers.new("decimate", "DECIMATE")
    dec.decimate_type = "COLLAPSE"
    dec.ratio = ratio
    bpy.ops.object.modifier_apply(modifier=dec.name)
    print(f"      {len(low.data.polygons):,} triangles (ratio {ratio:.4f})")
    bpy.ops.object.shade_smooth()

    print("[4/7] unwrapping low-poly")
    low.data.materials.clear()
    lowmat = bpy.data.materials.new("LOW_baked")
    lowmat.use_nodes = True
    low.data.materials.append(lowmat)
    unwrap(low)

    print("[5/7] baking")
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1          # we bake surface data, not lighting — 1 sample is enough
    scene.cycles.use_denoising = False
    extrusion = max(diag * 0.01, 0.001)

    nrm_img = new_image(f"{out.stem}_normal", args.texsize, is_data=True)
    nrm_node = bake_to_image(low, high, nrm_img, "NORMAL", extrusion)
    print("      normal map done")

    col_node = None
    if colour_attr and not args.no_color:
        col_img = new_image(f"{out.stem}_diffuse", args.texsize, is_data=False)
        col_node = bake_to_image(low, high, col_img, "DIFFUSE", extrusion)
        print("      diffuse map done")

    print("[6/7] wiring baked maps into the material")
    nt = lowmat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    nmap = nt.nodes.new("ShaderNodeNormalMap")
    nmap.location = (-250, -300)
    nrm_node.location = (-550, -300)
    nt.links.new(nrm_node.outputs["Color"], nmap.inputs["Color"])
    nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    if col_node:
        col_node.location = (-550, 100)
        nt.links.new(col_node.outputs["Color"], bsdf.inputs["Base Color"])
    # stone is matte: kill the default specular sheen so it doesn't look like plastic
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.85
    if "Metallic" in bsdf.inputs:
        bsdf.inputs["Metallic"].default_value = 0.0

    # the high-poly has done its job
    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    bpy.ops.object.delete()

    print(f"[7/7] exporting {out}")
    bpy.ops.object.select_all(action="DESELECT")
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    bpy.ops.export_scene.gltf(
        filepath=str(out),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_draco_mesh_compression_enable=True,   # ~5-10x smaller geometry, decoded in-browser
        export_draco_mesh_compression_level=6,
        export_image_format="JPEG",                  # textures dominate size; JPEG is fine for scans
        export_jpeg_quality=85,
    )

    if args.keep_blend:
        bpy.ops.wm.save_as_mainfile(filepath=args.keep_blend)

    mb = os.path.getsize(out) / 1e6
    print(f"\nDONE  {out}  {mb:.1f} MB  ({len(low.data.polygons):,} tris, {args.texsize}px maps)")
    if mb > 25:
        print("NOTE: >25 MB will load slowly on mobile. Re-run with --faces 30000 --texsize 1024.")


if __name__ == "__main__":
    main()
