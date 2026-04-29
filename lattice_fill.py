"""
TPMS Lattice Fill — Geometry Nodes builder
==========================================
Run inside Blender (Scripting tab → Open → Run Script).

What it does
------------
Creates a Geometry Nodes group called "TPMS Lattice Fill" that turns the
container mesh into a gyroid lattice clipped to that mesh's volume.

Pipeline:
    container mesh
        │
        ├─ Bounding Box (padded)
        │       │
        │       └─► Volume Cube with gyroid SDF ─► Volume to Mesh
        │                                               │
        └───────────────────────────────────────────────┴─► Boolean Intersect ─► out

Run with no selection → builds the node group only.
Run with an active mesh selected → also adds the modifier and sets defaults.

Tested against Blender 4.2 LTS API. Should work on 4.0+ and 5.x.
"""

import bpy

GROUP_NAME = "TPMS Lattice Fill"


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def new_node(tree, node_type, name, location):
    n = tree.nodes.new(node_type)
    n.name = name
    n.label = name
    n.location = location
    return n


def link(tree, from_socket, to_socket):
    tree.links.new(from_socket, to_socket)


def add_input(tree, socket_type, name, default=None, min_value=None, max_value=None):
    s = tree.interface.new_socket(name=name, in_out="INPUT", socket_type=socket_type)
    if default is not None:
        s.default_value = default
    if min_value is not None:
        s.min_value = min_value
    if max_value is not None:
        s.max_value = max_value
    return s


def add_output(tree, socket_type, name):
    return tree.interface.new_socket(name=name, in_out="OUTPUT", socket_type=socket_type)


def get_socket(node, display_name, kind="inputs"):
    """Find the FIRST ENABLED socket with this display name.

    Necessary for nodes like Switch where many sockets share a display name
    (one per data-type). After `node.input_type = "GEOMETRY"`, only the
    geometry variant has `.enabled == True`.
    """
    sockets = node.inputs if kind == "inputs" else node.outputs
    for s in sockets:
        if s.name == display_name and s.enabled:
            return s
    available = [s.name for s in sockets if s.enabled]
    raise KeyError(
        f"No enabled {kind} socket named '{display_name}' on '{node.name}'. "
        f"Available enabled: {available}"
    )


def math(tree, operation, name, location, value=None):
    """Single-value Math node (uses input[0], optional input[1])."""
    n = new_node(tree, "ShaderNodeMath", name, location)
    n.operation = operation
    if value is not None:
        n.inputs[1].default_value = value
    return n


# ────────────────────────────────────────────────────────────────────────────
# Build the node group
# ────────────────────────────────────────────────────────────────────────────

def build_group():
    # Wipe & recreate, so re-running the script always lands in a clean state.
    if GROUP_NAME in bpy.data.node_groups:
        bpy.data.node_groups.remove(bpy.data.node_groups[GROUP_NAME])
    g = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")

    # ── Interface (modifier inputs) ────────────────────────────────────────
    add_input(g, "NodeSocketGeometry", "Geometry")
    add_input(g, "NodeSocketVector",   "Cell Period",    default=(0.4, 0.4, 0.4),
              min_value=0.01, max_value=10.0)
    add_input(g, "NodeSocketFloat",    "Wall Thickness", default=0.05,
              min_value=0.001, max_value=2.0)
    add_input(g, "NodeSocketFloat",    "Voxel Size",     default=0.02,
              min_value=0.001, max_value=1.0)
    add_input(g, "NodeSocketFloat",    "Padding",        default=0.1,
              min_value=0.0,  max_value=5.0)
    mode_sock = add_input(g, "NodeSocketInt", "Mode", default=0,
                          min_value=0, max_value=2)
    mode_sock.description = "0 = Sheet (thin shell at g=0)   1 = Solid A (one labyrinth)   2 = Solid B (invert of A)"
    thr_sock  = add_input(g, "NodeSocketFloat", "Threshold", default=0.0,
                          min_value=-2.0, max_value=2.0)
    thr_sock.description = "Volume-to-Mesh isosurface offset. 0 = default. Positive shrinks, negative grows."
    add_input(g, "NodeSocketBool",     "Apply Boolean",  default=True)
    add_output(g, "NodeSocketGeometry", "Geometry")

    inp  = new_node(g, "NodeGroupInput",  "In",  (-1800,    0))
    outp = new_node(g, "NodeGroupOutput", "Out", ( 1600,    0))

    # ── 1. Bounding box of input geometry ──────────────────────────────────
    bbox = new_node(g, "GeometryNodeBoundBox", "BoundingBox", (-1500, 200))
    link(g, inp.outputs["Geometry"], bbox.inputs["Geometry"])

    # Pad min/max by ±Padding so the lattice sticks out before we boolean it.
    pad_min = new_node(g, "ShaderNodeVectorMath", "Pad Min", (-1300, 380))
    pad_min.operation = "SUBTRACT"
    pad_max = new_node(g, "ShaderNodeVectorMath", "Pad Max", (-1300, 220))
    pad_max.operation = "ADD"

    # Broadcast Padding (float) to a vector for the vector math:
    pad_vec = new_node(g, "ShaderNodeCombineXYZ", "Padding Vec", (-1500, 350))
    link(g, inp.outputs["Padding"], pad_vec.inputs["X"])
    link(g, inp.outputs["Padding"], pad_vec.inputs["Y"])
    link(g, inp.outputs["Padding"], pad_vec.inputs["Z"])

    link(g, bbox.outputs["Min"], pad_min.inputs[0])
    link(g, pad_vec.outputs["Vector"], pad_min.inputs[1])
    link(g, bbox.outputs["Max"], pad_max.inputs[0])
    link(g, pad_vec.outputs["Vector"], pad_max.inputs[1])

    # ── 2. Volume Cube spanning the padded bbox ────────────────────────────
    vol = new_node(g, "GeometryNodeVolumeCube", "Lattice Volume", (-700, 0))
    # Resolution from Voxel Size:
    #   res = ceil( (max - min) / voxel_size )
    bbox_size = new_node(g, "ShaderNodeVectorMath", "BBox Size", (-1100, 240))
    bbox_size.operation = "SUBTRACT"
    link(g, pad_max.outputs["Vector"], bbox_size.inputs[0])
    link(g, pad_min.outputs["Vector"], bbox_size.inputs[1])

    voxel_vec = new_node(g, "ShaderNodeCombineXYZ", "Voxel Vec", (-1100, 60))
    link(g, inp.outputs["Voxel Size"], voxel_vec.inputs["X"])
    link(g, inp.outputs["Voxel Size"], voxel_vec.inputs["Y"])
    link(g, inp.outputs["Voxel Size"], voxel_vec.inputs["Z"])

    res_div = new_node(g, "ShaderNodeVectorMath", "Resolution", (-900, 150))
    res_div.operation = "DIVIDE"
    link(g, bbox_size.outputs["Vector"], res_div.inputs[0])
    link(g, voxel_vec.outputs["Vector"], res_div.inputs[1])

    res_xyz = new_node(g, "ShaderNodeSeparateXYZ", "Res XYZ", (-700, 200))
    link(g, res_div.outputs["Vector"], res_xyz.inputs["Vector"])

    link(g, pad_min.outputs["Vector"], vol.inputs["Min"])
    link(g, pad_max.outputs["Vector"], vol.inputs["Max"])
    # Volume Cube resolution sockets are int — we connect float→int via socket.
    link(g, res_xyz.outputs["X"], vol.inputs["Resolution X"])
    link(g, res_xyz.outputs["Y"], vol.inputs["Resolution Y"])
    link(g, res_xyz.outputs["Z"], vol.inputs["Resolution Z"])

    # ── 3. Gyroid SDF as the volume's density field ────────────────────────
    # gyroid(p) = sin(px)·cos(py) + sin(py)·cos(pz) + sin(pz)·cos(px)
    # Surface = { p : gyroid(p) = 0 }.
    # Wall band: use   density = thickness − |gyroid(p_scaled)|
    # Volume to Mesh threshold = 0 → extracts the iso-band of half-width "thickness".
    pos = new_node(g, "GeometryNodeInputPosition", "Position", (-1100, -300))
    # Frequency = 2·pi / Cell Period   (one period per Cell Period units)
    twopi = new_node(g, "ShaderNodeValue", "TwoPi", (-1100, -460))
    twopi.outputs[0].default_value = 6.28318530718

    period_xyz = new_node(g, "ShaderNodeSeparateXYZ", "Period XYZ", (-1100, -550))
    link(g, inp.outputs["Cell Period"], period_xyz.inputs["Vector"])

    fx = math(g, "DIVIDE", "fx", (-900, -460));  link(g, twopi.outputs[0], fx.inputs[0]); link(g, period_xyz.outputs["X"], fx.inputs[1])
    fy = math(g, "DIVIDE", "fy", (-900, -560));  link(g, twopi.outputs[0], fy.inputs[0]); link(g, period_xyz.outputs["Y"], fy.inputs[1])
    fz = math(g, "DIVIDE", "fz", (-900, -660));  link(g, twopi.outputs[0], fz.inputs[0]); link(g, period_xyz.outputs["Z"], fz.inputs[1])

    pos_xyz = new_node(g, "ShaderNodeSeparateXYZ", "Pos XYZ", (-900, -300))
    link(g, pos.outputs["Position"], pos_xyz.inputs["Vector"])

    # px = x·fx, py = y·fy, pz = z·fz
    px = math(g, "MULTIPLY", "px", (-700, -300)); link(g, pos_xyz.outputs["X"], px.inputs[0]); link(g, fx.outputs[0], px.inputs[1])
    py = math(g, "MULTIPLY", "py", (-700, -400)); link(g, pos_xyz.outputs["Y"], py.inputs[0]); link(g, fy.outputs[0], py.inputs[1])
    pz = math(g, "MULTIPLY", "pz", (-700, -500)); link(g, pos_xyz.outputs["Z"], pz.inputs[0]); link(g, fz.outputs[0], pz.inputs[1])

    sx = math(g, "SINE",   "sin(px)", (-500, -250)); link(g, px.outputs[0], sx.inputs[0])
    cx = math(g, "COSINE", "cos(px)", (-500, -350)); link(g, px.outputs[0], cx.inputs[0])
    sy = math(g, "SINE",   "sin(py)", (-500, -450)); link(g, py.outputs[0], sy.inputs[0])
    cy = math(g, "COSINE", "cos(py)", (-500, -550)); link(g, py.outputs[0], cy.inputs[0])
    sz = math(g, "SINE",   "sin(pz)", (-500, -650)); link(g, pz.outputs[0], sz.inputs[0])
    cz = math(g, "COSINE", "cos(pz)", (-500, -750)); link(g, pz.outputs[0], cz.inputs[0])

    sxcy = math(g, "MULTIPLY", "sx·cy", (-300, -300)); link(g, sx.outputs[0], sxcy.inputs[0]); link(g, cy.outputs[0], sxcy.inputs[1])
    sycz = math(g, "MULTIPLY", "sy·cz", (-300, -450)); link(g, sy.outputs[0], sycz.inputs[0]); link(g, cz.outputs[0], sycz.inputs[1])
    szcx = math(g, "MULTIPLY", "sz·cx", (-300, -600)); link(g, sz.outputs[0], szcx.inputs[0]); link(g, cx.outputs[0], szcx.inputs[1])

    sum1 = math(g, "ADD", "sum1", (-100, -380)); link(g, sxcy.outputs[0], sum1.inputs[0]); link(g, sycz.outputs[0], sum1.inputs[1])
    gyr  = math(g, "ADD", "gyroid", (-100, -550)); link(g, sum1.outputs[0], gyr.inputs[0]);  link(g, szcx.outputs[0], gyr.inputs[1])

    # ── Three density variants, picked by Mode ─────────────────────────────
    #   Mode 0 (Sheet)   : density = t − |g|        → band of half-width t around g=0
    #   Mode 1 (Solid A) : density = t − g          → fills g<0 labyrinth
    #   Mode 2 (Solid B) : density = t + g          → fills g>0 labyrinth (= "invert")
    abs_g = math(g, "ABSOLUTE", "|gyroid|", (100, -550))
    link(g, gyr.outputs[0], abs_g.inputs[0])

    d_sheet = math(g, "SUBTRACT", "d_sheet", (300, -400))
    link(g, inp.outputs["Wall Thickness"], d_sheet.inputs[0])
    link(g, abs_g.outputs[0],              d_sheet.inputs[1])

    d_solid_a = math(g, "SUBTRACT", "d_solidA", (300, -560))
    link(g, inp.outputs["Wall Thickness"], d_solid_a.inputs[0])
    link(g, gyr.outputs[0],                d_solid_a.inputs[1])

    d_solid_b = math(g, "ADD", "d_solidB", (300, -720))
    link(g, inp.outputs["Wall Thickness"], d_solid_b.inputs[0])
    link(g, gyr.outputs[0],                d_solid_b.inputs[1])

    # Compare Mode==1 and Mode==2 (epsilon 0.5 gives clean integer test).
    is_mode1 = math(g, "COMPARE", "Mode==1", (500, -500), value=1.0)
    link(g, inp.outputs["Mode"], is_mode1.inputs[0])
    is_mode1.inputs[2].default_value = 0.5

    is_mode2 = math(g, "COMPARE", "Mode==2", (500, -660), value=2.0)
    link(g, inp.outputs["Mode"], is_mode2.inputs[0])
    is_mode2.inputs[2].default_value = 0.5

    # Cascade: if Mode==1 use Solid A, elif Mode==2 use Solid B, else Sheet.
    sw_mode_a = new_node(g, "GeometryNodeSwitch", "Pick Mode A?", (700, -480))
    sw_mode_a.input_type = "FLOAT"
    link(g, is_mode1.outputs[0],   get_socket(sw_mode_a, "Switch"))
    link(g, d_sheet.outputs[0],    get_socket(sw_mode_a, "False"))
    link(g, d_solid_a.outputs[0],  get_socket(sw_mode_a, "True"))

    sw_mode_b = new_node(g, "GeometryNodeSwitch", "Pick Mode B?", (900, -560))
    sw_mode_b.input_type = "FLOAT"
    link(g, is_mode2.outputs[0],                       get_socket(sw_mode_b, "Switch"))
    link(g, get_socket(sw_mode_a, "Output", "outputs"), get_socket(sw_mode_b, "False"))
    link(g, d_solid_b.outputs[0],                       get_socket(sw_mode_b, "True"))

    # Feed selected density into Volume Cube
    link(g, get_socket(sw_mode_b, "Output", "outputs"), vol.inputs["Density"])

    # ── 4. Volume to Mesh ──────────────────────────────────────────────────
    # Default mode uses the input volume's own grid — and we already
    # configured that grid via Volume Cube's Resolution X/Y/Z above.
    # No need to set resolution_mode (which was removed in newer Blender)
    # or to re-pipe Voxel Size here; that would just resample redundantly.
    v2m = new_node(g, "GeometryNodeVolumeToMesh", "Volume to Mesh", (500, 0))
    link(g, vol.outputs["Volume"], v2m.inputs["Volume"])
    # Threshold is now exposed to the modifier so you can fine-tune the
    # iso-surface live without re-running the script.
    link(g, inp.outputs["Threshold"], v2m.inputs["Threshold"])

    # ── 5. Optional Boolean intersect with original geometry ───────────────
    # In INTERSECT mode the Mesh Boolean node uses ONLY the "Mesh 2" multi-input;
    # "Mesh 1" is exclusive to DIFFERENCE. Both meshes therefore feed Mesh 2.
    bool_node = new_node(g, "GeometryNodeMeshBoolean", "Intersect", (900, 0))
    bool_node.operation = "INTERSECT"
    link(g, inp.outputs["Geometry"], bool_node.inputs["Mesh 2"])
    link(g, v2m.outputs["Mesh"],     bool_node.inputs["Mesh 2"])

    # Switch between {raw lattice} and {boolean intersect} based on Apply Boolean.
    # Using named sockets keeps the script stable across Blender 4.x / 5.x.
    sw = new_node(g, "GeometryNodeSwitch", "Apply?", (1200, 0))
    sw.input_type = "GEOMETRY"
    link(g, inp.outputs["Apply Boolean"], get_socket(sw, "Switch"))
    link(g, v2m.outputs["Mesh"],          get_socket(sw, "False"))
    link(g, bool_node.outputs["Mesh"],    get_socket(sw, "True"))

    # ── 6. Smooth shading & output ─────────────────────────────────────────
    smooth = new_node(g, "GeometryNodeSetShadeSmooth", "Shade Smooth", (1400, 0))
    link(g, get_socket(sw, "Output", "outputs"), smooth.inputs["Geometry"])
    link(g, smooth.outputs["Geometry"],          outp.inputs["Geometry"])

    return g


# ────────────────────────────────────────────────────────────────────────────
# Apply to active object
# ────────────────────────────────────────────────────────────────────────────

def apply_to_active(group):
    obj = bpy.context.active_object
    if obj is None or obj.type != "MESH":
        print("[lattice_fill] No active mesh — node group built. "
              "Select a mesh and re-run, or add 'TPMS Lattice Fill' as a "
              "Geometry Nodes modifier manually.")
        return

    # Remove any existing modifier with our name so re-runs are idempotent.
    for m in list(obj.modifiers):
        if m.type == "NODES" and m.name == GROUP_NAME:
            obj.modifiers.remove(m)

    mod = obj.modifiers.new(name=GROUP_NAME, type="NODES")
    mod.node_group = group
    print(f"[lattice_fill] Modifier added to '{obj.name}'.")


def show_popup(title, lines):
    """Pop a Blender error dialog so the user sees failures without the console."""
    def draw(self, _ctx):
        for ln in lines:
            self.layout.label(text=ln)
    bpy.context.window_manager.popup_menu(draw, title=title, icon="ERROR")


def main():
    print("=" * 60)
    print(f"[lattice_fill] Blender {bpy.app.version_string}")
    print(f"[lattice_fill] Building node group '{GROUP_NAME}'...")
    try:
        g = build_group()
        print(f"[lattice_fill] OK — node group built ({len(g.nodes)} nodes).")
        apply_to_active(g)
        print(f"[lattice_fill] DONE.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("[lattice_fill] *** FAILED ***")
        print(tb)
        show_popup("TPMS Lattice Fill — build failed",
                   [f"{type(e).__name__}: {e}",
                    "See System Console for full traceback."])
        raise


if __name__ == "__main__":
    main()
