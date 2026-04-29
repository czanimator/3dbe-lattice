"""
3dbe Lattice — Geometry Nodes builder
=====================================
Run inside Blender (Scripting tab → Open → Run Script).
Will be packaged as a proper Blender addon in a follow-up step.

What it does
------------
Creates a Geometry Nodes group called "3dbe Lattice" that fills a custom
container mesh with a parametric lattice — selectable between TPMS surfaces
(Gyroid, Schwarz-P, Schwarz-D, Neovius) and a Beam Grid SDF.

Pipeline:
    container mesh
        │
        ├─ Bounding Box (padded)
        │       │
        │       └─► Volume Cube with chosen field SDF ─┐
        │                                              │
        └─► Geometry Proximity + Sample Normal ────────┤
                                                       │ MIN(field, mesh-inside)
                                                       ▼
                                              Volume to Mesh ─► Smooth ─► out

No mesh boolean — clipping is implicit in the SDF density combination.

Run with no selection → builds the node group only.
Run with an active mesh selected → also adds the modifier and sets defaults.

Tested against Blender 4.2 LTS API. Should work on 4.0+ and 5.x.
"""

import bpy

GROUP_NAME = "3dbe Lattice"
LOG_TAG    = "[3dbe_lattice]"


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
    clip_sock = add_input(g, "NodeSocketBool", "Clip to Mesh", default=True)
    clip_sock.description = "Mask the lattice density to the inside of the input mesh (SDF-based, no boolean)."
    flip_sock = add_input(g, "NodeSocketBool", "Flip Mesh Normals", default=False)
    flip_sock.description = "Enable if the inside-test is inverted (lattice outside mesh, hole inside) — your mesh has inward normals. Cleaner fix: Shift+N in Edit Mode to recalculate outward."
    flipout_sock = add_input(g, "NodeSocketBool", "Flip Output", default=False)
    flipout_sock.description = "Multiply the final density by −1 right before Volume to Mesh. Last-resort fix when SDF math produces an inverted iso-surface that Flip Mesh Normals alone doesn't correct (e.g. deeply non-manifold input)."
    pat_sock = add_input(g, "NodeSocketInt", "Pattern", default=0,
                         min_value=0, max_value=4)
    pat_sock.description = "0=Gyroid  1=Schwarz-P  2=Schwarz-D (Diamond)  3=Neovius  4=Beam Grid"
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

    # ── 3b. Other TPMS field formulas (reuse sx/cx/sy/cy/sz/cz) ────────────
    # Schwarz-P:  cx + cy + cz
    sp_sum1 = math(g, "ADD", "cx+cy", (-100, -880))
    link(g, cx.outputs[0], sp_sum1.inputs[0]); link(g, cy.outputs[0], sp_sum1.inputs[1])
    schwarz_p = math(g, "ADD", "schwarz_p", (-100, -960))
    link(g, sp_sum1.outputs[0], schwarz_p.inputs[0]); link(g, cz.outputs[0], schwarz_p.inputs[1])

    # Schwarz-D (Diamond):  sx·sy·sz + sx·cy·cz + cx·sy·cz + cx·cy·sz
    sxsy   = math(g, "MULTIPLY", "sx·sy", (-300, -1500)); link(g, sx.outputs[0], sxsy.inputs[0]);   link(g, sy.outputs[0], sxsy.inputs[1])
    sxcy_d = math(g, "MULTIPLY", "sx·cy", (-300, -1580)); link(g, sx.outputs[0], sxcy_d.inputs[0]); link(g, cy.outputs[0], sxcy_d.inputs[1])
    cxsy   = math(g, "MULTIPLY", "cx·sy", (-300, -1660)); link(g, cx.outputs[0], cxsy.inputs[0]);   link(g, sy.outputs[0], cxsy.inputs[1])
    cxcy   = math(g, "MULTIPLY", "cx·cy", (-300, -1740)); link(g, cx.outputs[0], cxcy.inputs[0]);   link(g, cy.outputs[0], cxcy.inputs[1])

    t1 = math(g, "MULTIPLY", "sx·sy·sz", (-100, -1500)); link(g, sxsy.outputs[0],   t1.inputs[0]); link(g, sz.outputs[0], t1.inputs[1])
    t2 = math(g, "MULTIPLY", "sx·cy·cz", (-100, -1580)); link(g, sxcy_d.outputs[0], t2.inputs[0]); link(g, cz.outputs[0], t2.inputs[1])
    t3 = math(g, "MULTIPLY", "cx·sy·cz", (-100, -1660)); link(g, cxsy.outputs[0],   t3.inputs[0]); link(g, cz.outputs[0], t3.inputs[1])
    t4 = math(g, "MULTIPLY", "cx·cy·sz", (-100, -1740)); link(g, cxcy.outputs[0],   t4.inputs[0]); link(g, sz.outputs[0], t4.inputs[1])

    sd_sum1 = math(g, "ADD", "t1+t2", (100, -1540)); link(g, t1.outputs[0], sd_sum1.inputs[0]); link(g, t2.outputs[0], sd_sum1.inputs[1])
    sd_sum2 = math(g, "ADD", "t3+t4", (100, -1700)); link(g, t3.outputs[0], sd_sum2.inputs[0]); link(g, t4.outputs[0], sd_sum2.inputs[1])
    schwarz_d = math(g, "ADD", "schwarz_d", (300, -1620))
    link(g, sd_sum1.outputs[0], schwarz_d.inputs[0]); link(g, sd_sum2.outputs[0], schwarz_d.inputs[1])

    # Neovius:  3·(cx+cy+cz) + 4·(cx·cy·cz)
    sp_x3 = math(g, "MULTIPLY", "3·sp", (100, -2000), value=3.0)
    link(g, schwarz_p.outputs[0], sp_x3.inputs[0])
    cxcycz = math(g, "MULTIPLY", "cx·cy·cz", (100, -2080))
    link(g, cxcy.outputs[0], cxcycz.inputs[0]); link(g, cz.outputs[0], cxcycz.inputs[1])
    cxcycz_x4 = math(g, "MULTIPLY", "4·cx·cy·cz", (300, -2080), value=4.0)
    link(g, cxcycz.outputs[0], cxcycz_x4.inputs[0])
    neovius = math(g, "ADD", "neovius", (500, -2040))
    link(g, sp_x3.outputs[0], neovius.inputs[0]); link(g, cxcycz_x4.outputs[0], neovius.inputs[1])

    # ── 3c. Beam Grid SDF (axis-aligned cubic strut lattice) ───────────────
    # For each voxel, wrap into one cell:  l = floored_mod(p, period) − period/2
    # Distance to nearest X-axis strut centerline = √(ly² + lz²);
    # min over the three axes, minus Wall Thickness (used as strut radius).
    period_half_x = math(g, "MULTIPLY", "px/2", (-1500, -2480), value=0.5); link(g, period_xyz.outputs["X"], period_half_x.inputs[0])
    period_half_y = math(g, "MULTIPLY", "py/2", (-1500, -2560), value=0.5); link(g, period_xyz.outputs["Y"], period_half_y.inputs[0])
    period_half_z = math(g, "MULTIPLY", "pz/2", (-1500, -2640), value=0.5); link(g, period_xyz.outputs["Z"], period_half_z.inputs[0])

    mod_x = math(g, "FLOORED_MODULO", "x mod px", (-1300, -2480)); link(g, pos_xyz.outputs["X"], mod_x.inputs[0]); link(g, period_xyz.outputs["X"], mod_x.inputs[1])
    mod_y = math(g, "FLOORED_MODULO", "y mod py", (-1300, -2560)); link(g, pos_xyz.outputs["Y"], mod_y.inputs[0]); link(g, period_xyz.outputs["Y"], mod_y.inputs[1])
    mod_z = math(g, "FLOORED_MODULO", "z mod pz", (-1300, -2640)); link(g, pos_xyz.outputs["Z"], mod_z.inputs[0]); link(g, period_xyz.outputs["Z"], mod_z.inputs[1])

    lx = math(g, "SUBTRACT", "lx", (-1100, -2480)); link(g, mod_x.outputs[0], lx.inputs[0]); link(g, period_half_x.outputs[0], lx.inputs[1])
    ly = math(g, "SUBTRACT", "ly", (-1100, -2560)); link(g, mod_y.outputs[0], ly.inputs[0]); link(g, period_half_y.outputs[0], ly.inputs[1])
    lz = math(g, "SUBTRACT", "lz", (-1100, -2640)); link(g, mod_z.outputs[0], lz.inputs[0]); link(g, period_half_z.outputs[0], lz.inputs[1])

    zero_val = new_node(g, "ShaderNodeValue", "0", (-900, -2780))
    zero_val.outputs[0].default_value = 0.0

    vec_yz = new_node(g, "ShaderNodeCombineXYZ", "(0,ly,lz)", (-900, -2480))
    link(g, zero_val.outputs[0], vec_yz.inputs["X"]); link(g, ly.outputs[0], vec_yz.inputs["Y"]); link(g, lz.outputs[0], vec_yz.inputs["Z"])
    vec_xz = new_node(g, "ShaderNodeCombineXYZ", "(lx,0,lz)", (-900, -2560))
    link(g, lx.outputs[0], vec_xz.inputs["X"]); link(g, zero_val.outputs[0], vec_xz.inputs["Y"]); link(g, lz.outputs[0], vec_xz.inputs["Z"])
    vec_xy = new_node(g, "ShaderNodeCombineXYZ", "(lx,ly,0)", (-900, -2640))
    link(g, lx.outputs[0], vec_xy.inputs["X"]); link(g, ly.outputs[0], vec_xy.inputs["Y"]); link(g, zero_val.outputs[0], vec_xy.inputs["Z"])

    len_yz = new_node(g, "ShaderNodeVectorMath", "|yz|", (-700, -2480)); len_yz.operation = "LENGTH"; link(g, vec_yz.outputs["Vector"], len_yz.inputs[0])
    len_xz = new_node(g, "ShaderNodeVectorMath", "|xz|", (-700, -2560)); len_xz.operation = "LENGTH"; link(g, vec_xz.outputs["Vector"], len_xz.inputs[0])
    len_xy = new_node(g, "ShaderNodeVectorMath", "|xy|", (-700, -2640)); len_xy.operation = "LENGTH"; link(g, vec_xy.outputs["Vector"], len_xy.inputs[0])

    min1 = math(g, "MINIMUM", "min(yz,xz)", (-500, -2520)); link(g, len_yz.outputs["Value"], min1.inputs[0]); link(g, len_xz.outputs["Value"], min1.inputs[1])
    min_all = math(g, "MINIMUM", "min(...,xy)", (-300, -2580)); link(g, min1.outputs[0], min_all.inputs[0]); link(g, len_xy.outputs["Value"], min_all.inputs[1])

    beam_sdf = math(g, "SUBTRACT", "beam_sdf", (-100, -2580))
    link(g, min_all.outputs[0],            beam_sdf.inputs[0])
    link(g, inp.outputs["Wall Thickness"], beam_sdf.inputs[1])

    # ── 3d. Pattern selector — cascade of FLOAT switches ───────────────────
    # Default = Gyroid; each Switch overrides if Pattern matches (1..4).
    is_p1 = math(g, "COMPARE", "Pattern==1", (200, -150), value=1.0); link(g, inp.outputs["Pattern"], is_p1.inputs[0]); is_p1.inputs[2].default_value = 0.5
    is_p2 = math(g, "COMPARE", "Pattern==2", (200, -240), value=2.0); link(g, inp.outputs["Pattern"], is_p2.inputs[0]); is_p2.inputs[2].default_value = 0.5
    is_p3 = math(g, "COMPARE", "Pattern==3", (200, -330), value=3.0); link(g, inp.outputs["Pattern"], is_p3.inputs[0]); is_p3.inputs[2].default_value = 0.5
    is_p4 = math(g, "COMPARE", "Pattern==4", (200, -420), value=4.0); link(g, inp.outputs["Pattern"], is_p4.inputs[0]); is_p4.inputs[2].default_value = 0.5

    def _pat_switch(name, prev_socket, alt_socket, sw_bool, location):
        sw = new_node(g, "GeometryNodeSwitch", name, location)
        sw.input_type = "FLOAT"
        link(g, sw_bool,     get_socket(sw, "Switch"))
        link(g, prev_socket, get_socket(sw, "False"))
        link(g, alt_socket,  get_socket(sw, "True"))
        return sw

    sw_p1 = _pat_switch("Pat=Schwarz-P?", gyr.outputs[0],                                schwarz_p.outputs[0], is_p1.outputs[0], (400, -150))
    sw_p2 = _pat_switch("Pat=Schwarz-D?", get_socket(sw_p1, "Output", "outputs"),        schwarz_d.outputs[0], is_p2.outputs[0], (550, -240))
    sw_p3 = _pat_switch("Pat=Neovius?",   get_socket(sw_p2, "Output", "outputs"),        neovius.outputs[0],   is_p3.outputs[0], (700, -330))
    sw_p4 = _pat_switch("Pat=Beam?",      get_socket(sw_p3, "Output", "outputs"),        beam_sdf.outputs[0],  is_p4.outputs[0], (850, -420))
    selected_field = get_socket(sw_p4, "Output", "outputs")

    # ── Three density variants, picked by Mode ─────────────────────────────
    # Operates on the selected field f (the Pattern dispatch above):
    #   Mode 0 (Sheet)   : density = t − |f|   → band of half-width t around f=0
    #   Mode 1 (Solid A) : density = t − f     → fills f<0 region
    #   Mode 2 (Solid B) : density = t + f     → fills f>0 region (= "invert")
    # Beam Grid pattern: f is a SDF with surface at f=0 (negative inside struts);
    # use Mode 1 for solid struts of effective radius ≈ 2·Wall Thickness.
    abs_f = math(g, "ABSOLUTE", "|field|", (1050, -550))
    link(g, selected_field, abs_f.inputs[0])

    d_sheet = math(g, "SUBTRACT", "d_sheet", (1250, -400))
    link(g, inp.outputs["Wall Thickness"], d_sheet.inputs[0])
    link(g, abs_f.outputs[0],              d_sheet.inputs[1])

    d_solid_a = math(g, "SUBTRACT", "d_solidA", (1250, -560))
    link(g, inp.outputs["Wall Thickness"], d_solid_a.inputs[0])
    link(g, selected_field,                d_solid_a.inputs[1])

    d_solid_b = math(g, "ADD", "d_solidB", (1250, -720))
    link(g, inp.outputs["Wall Thickness"], d_solid_b.inputs[0])
    link(g, selected_field,                d_solid_b.inputs[1])

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

    # ── SDF mask: combine lattice density with "inside-mesh" indicator ─────
    # Per voxel:
    #   1. Geometry Proximity → nearest point Q on the input mesh surface
    #   2. Sample Nearest Surface → mesh normal N at Q
    #   3. signed_dist = (P − Q) · N    (negative when P is inside mesh)
    #   4. inside_density = − signed_dist  (positive inside, like a thin SDF)
    #   5. combined_density = MIN(lattice_density, inside_density)
    # The MIN is the SDF intersection: solid only where BOTH the lattice
    # band is solid AND we're inside the mesh. Volume to Mesh then extracts
    # both surfaces in a single pass — no separate boolean required.
    prox = new_node(g, "GeometryNodeProximity", "Mesh Proximity", (-700, -1100))
    prox.target_element = "FACES"
    link(g, inp.outputs["Geometry"], prox.inputs["Target"])
    # Explicitly wire voxel Position into Source Position. Some Blender
    # builds expose this as an input socket (4.1+); older builds default
    # to implicit field Position. Connecting it explicitly is harmless
    # and guarantees per-voxel evaluation.
    for s in prox.inputs:
        if s.name == "Source Position":
            link(g, pos.outputs["Position"], s)
            break

    input_normal = new_node(g, "GeometryNodeInputNormal", "Mesh Normal Field",
                            (-1100, -1100))

    sns = new_node(g, "GeometryNodeSampleNearestSurface", "Sample Normal at Q",
                   (-700, -1300))
    sns.data_type = "FLOAT_VECTOR"
    link(g, inp.outputs["Geometry"], sns.inputs["Mesh"])
    link(g, input_normal.outputs["Normal"], sns.inputs["Value"])
    # Sample Position: same situation — explicit connect for robustness.
    for s in sns.inputs:
        if s.name == "Sample Position":
            link(g, pos.outputs["Position"], s)
            break

    # direction = P − Q
    direction = new_node(g, "ShaderNodeVectorMath", "P − Q", (-450, -1150))
    direction.operation = "SUBTRACT"
    link(g, pos.outputs["Position"],     direction.inputs[0])
    link(g, prox.outputs["Position"],    direction.inputs[1])

    # signed_dist = direction · normal
    signed_dot = new_node(g, "ShaderNodeVectorMath", "Dot(dir, normal)",
                          (-200, -1200))
    signed_dot.operation = "DOT_PRODUCT"
    link(g, direction.outputs["Vector"], signed_dot.inputs[0])
    link(g, sns.outputs["Value"],        signed_dot.inputs[1])

    # inside_density: positive inside mesh, negative outside.
    # With outward normals (Blender default):  inside_density = − signed_dist
    # With inward normals (flipped):           inside_density = + signed_dist
    # The "Flip Mesh Normals" toggle picks between the two.
    inside_outward = math(g, "MULTIPLY", "−signed_dot", (50, -1250), value=-1.0)
    link(g, signed_dot.outputs["Value"], inside_outward.inputs[0])

    flip_switch = new_node(g, "GeometryNodeSwitch", "Flip Normals?", (250, -1200))
    flip_switch.input_type = "FLOAT"
    link(g, inp.outputs["Flip Mesh Normals"], get_socket(flip_switch, "Switch"))
    link(g, inside_outward.outputs[0],        get_socket(flip_switch, "False"))
    link(g, signed_dot.outputs["Value"],      get_socket(flip_switch, "True"))

    # combined = MIN(lattice_density, inside_density)
    combined = math(g, "MINIMUM", "MIN(lattice, mesh)", (450, -1050))
    link(g, get_socket(sw_mode_b, "Output", "outputs"),    combined.inputs[0])
    link(g, get_socket(flip_switch, "Output", "outputs"),  combined.inputs[1])

    # Switch between {lattice only} and {clipped to mesh} based on Clip to Mesh
    sw_clip = new_node(g, "GeometryNodeSwitch", "Clip?", (550, -900))
    sw_clip.input_type = "FLOAT"
    link(g, inp.outputs["Clip to Mesh"],                    get_socket(sw_clip, "Switch"))
    link(g, get_socket(sw_mode_b, "Output", "outputs"),     get_socket(sw_clip, "False"))
    link(g, combined.outputs[0],                            get_socket(sw_clip, "True"))

    # ── Flip Output: optional final-density × −1 ───────────────────────────
    # Last-resort fix when SDF math produces an inverted iso-surface that
    # the Flip Mesh Normals toggle alone doesn't correct (typically for
    # deeply non-manifold or open-shell input meshes).
    clipped_density = get_socket(sw_clip, "Output", "outputs")
    flipped_out = math(g, "MULTIPLY", "Final ×−1", (700, -950), value=-1.0)
    link(g, clipped_density, flipped_out.inputs[0])

    sw_flipout = new_node(g, "GeometryNodeSwitch", "Flip Output?", (900, -880))
    sw_flipout.input_type = "FLOAT"
    link(g, inp.outputs["Flip Output"], get_socket(sw_flipout, "Switch"))
    link(g, clipped_density,            get_socket(sw_flipout, "False"))
    link(g, flipped_out.outputs[0],     get_socket(sw_flipout, "True"))

    # Feed final density into Volume Cube.
    link(g, get_socket(sw_flipout, "Output", "outputs"), vol.inputs["Density"])

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

    # ── 5. Smooth shading & output ─────────────────────────────────────────
    # No boolean needed — Volume to Mesh already produced the clipped result
    # because the density field was MIN-combined with the mesh-inside SDF.
    smooth = new_node(g, "GeometryNodeSetShadeSmooth", "Shade Smooth", (900, 0))
    link(g, v2m.outputs["Mesh"],         smooth.inputs["Geometry"])
    link(g, smooth.outputs["Geometry"],  outp.inputs["Geometry"])

    return g


# ────────────────────────────────────────────────────────────────────────────
# Apply to active object
# ────────────────────────────────────────────────────────────────────────────

def apply_to_active(group):
    obj = bpy.context.active_object
    if obj is None or obj.type != "MESH":
        print(f"[3dbe_lattice] No active mesh — node group built. "
              f"Select a mesh and re-run, or add '{GROUP_NAME}' as a "
              f"Geometry Nodes modifier manually.")
        return

    # Remove any existing modifier with our name so re-runs are idempotent.
    for m in list(obj.modifiers):
        if m.type == "NODES" and m.name == GROUP_NAME:
            obj.modifiers.remove(m)

    mod = obj.modifiers.new(name=GROUP_NAME, type="NODES")
    mod.node_group = group
    print(f"[3dbe_lattice] Modifier added to '{obj.name}'.")


def show_popup(title, lines):
    """Pop a Blender error dialog so the user sees failures without the console."""
    def draw(self, _ctx):
        for ln in lines:
            self.layout.label(text=ln)
    bpy.context.window_manager.popup_menu(draw, title=title, icon="ERROR")


def main():
    print("=" * 60)
    print(f"[3dbe_lattice] Blender {bpy.app.version_string}")
    print(f"[3dbe_lattice] Building node group '{GROUP_NAME}'...")
    try:
        g = build_group()
        print(f"[3dbe_lattice] OK — node group built ({len(g.nodes)} nodes).")
        apply_to_active(g)
        print(f"[3dbe_lattice] DONE.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("[3dbe_lattice] *** FAILED ***")
        print(tb)
        show_popup(f"{GROUP_NAME} — build failed",
                   [f"{type(e).__name__}: {e}",
                    "See System Console for full traceback."])
        raise


if __name__ == "__main__":
    main()
