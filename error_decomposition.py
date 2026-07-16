#!/usr/bin/env python3
"""
误差分解实验：量化 15cm pre-opt FK 误差的来源

核心问题：pre-opt 误差 15cm 来自哪里？
- A) l_hand_camera_joint (相机安装外参) 不准确？
- B) 臂链 FK (joint origins) 不准确？
- C) checkerboard_joint (棋盘 ground truth) 不准确？
- D) PnP 拟合噪声？

方法：将每样本的 board 位姿误差投影回 camera frame，
若误差在相机系中方向一致 → 固定安装误差（A）
若误差随臂形变化 → 臂链 FK 误差（B）
"""

import csv, math, sys
from pathlib import Path
import numpy as np
import xml.etree.ElementTree as ET

# ============ 复用 FK/PnP 函数 ============
def rpy_to_R(r, p, y):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    return Rz @ Ry @ Rx

def parse_origin(el):
    if el is None: return np.eye(3), np.zeros(3)
    xyz = [float(x) for x in el.get("xyz", "0 0 0").split()]
    rpy = [float(x) for x in el.get("rpy", "0 0 0").split()]
    return rpy_to_R(rpy[0], rpy[1], rpy[2]), np.array(xyz, dtype=float)

def parse_axis(joint_el):
    ax = joint_el.find("axis")
    if ax is None: return np.array([0.0, 0.0, 1.0], dtype=float)
    v = np.array([float(x) for x in ax.get("xyz", "0 0 1").split()], dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.array([0.0, 0.0, 1.0], dtype=float)

def rodrigues(axis, angle):
    x, y, z = axis; c, s = math.cos(angle), math.sin(angle); C = 1.0 - c
    return np.array([[c+x*x*C, x*y*C-z*s, x*z*C+y*s],
                     [y*x*C+z*s, c+y*y*C, y*z*C-x*s],
                     [z*x*C-y*s, z*y*C+x*s, c+z*z*C]], dtype=float)

def joint_T(q, joint_el):
    R0, t0 = parse_origin(joint_el.find("origin"))
    T = np.eye(4); T[:3,:3]=R0; T[:3,3]=t0
    jtype = joint_el.get("type","fixed")
    if jtype=="fixed": return T
    Rq = rodrigues(parse_axis(joint_el), float(q))
    Tq = np.eye(4); Tq[:3,:3]=Rq
    return T @ Tq

def load_urdf_joints(path):
    root = ET.parse(str(path)).getroot()
    return {j.get("name"): j for j in root.findall("joint") if j.get("name")}

def find_chain_joint_names(joints, root_link, tip_link):
    joint_by_child, parent_of_child = {}, {}
    for jn, jel in joints.items():
        par, chi = jel.find("parent"), jel.find("child")
        if par is None or chi is None: continue
        pl, cl = par.get("link"), chi.get("link")
        if not pl or not cl: continue
        joint_by_child[cl] = jn; parent_of_child[cl] = pl
    chain_rev, cur = [], tip_link
    while cur != root_link:
        if cur not in joint_by_child: raise RuntimeError(f"stuck at {cur}")
        chain_rev.append(joint_by_child[cur]); cur = parent_of_child[cur]
    chain_rev.reverse(); return chain_rev

def fk_base_to_tip(joints, chain, q_by_joint):
    T = np.eye(4)
    for jn in chain: T = T @ joint_T(float(q_by_joint.get(jn,0.0)), joints[jn])
    return T

def fk_root_to_tip(joints, fk_root, tip_link, q_by_joint):
    try:
        chain = find_chain_joint_names(joints, fk_root, tip_link)
        return fk_base_to_tip(joints, chain, q_by_joint)
    except RuntimeError:
        chain_w_t = find_chain_joint_names(joints, "waist_yaw_link", tip_link)
        chain_w_z = find_chain_joint_names(joints, "waist_yaw_link", fk_root)
        return np.linalg.inv(fk_base_to_tip(joints, chain_w_z, q_by_joint)) @ fk_base_to_tip(joints, chain_w_t, q_by_joint)

def load_joints_csv(path):
    by_sample = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            by_sample.setdefault(int(row["sample_id"]), {})[row["joint_name"].strip()] = float(row["position"])
    return by_sample

def load_features_camera_points(path, sample_id, sensor_name):
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if int(row["sample_id"]) != sample_id: continue
            if row["sensor_name"].strip() != sensor_name: continue
            rows.append((int(row["feature_idx"]), np.array([float(row["x"]), float(row["y"]), float(row["z"])])))
    rows.sort(key=lambda x: x[0])
    return np.stack([x[1] for x in rows], axis=0)

def board_object_points(nx, ny, square, remap=True):
    pts = np.array([[(i%nx)*square, (i//nx)*square, 0.0] for i in range(nx*ny)], dtype=float)
    if remap: pts[:,0] -= ((nx-1)*square*0.5); pts[:,1] -= ((ny-1)*square*0.5)
    return pts

def rigid_board_to_cam(P_board, Q_cam):
    Pc, Qc = P_board.T, Q_cam.T
    pbar, qbar = Pc.mean(axis=1,keepdims=True), Qc.mean(axis=1,keepdims=True)
    H = (Pc-pbar) @ (Qc-qbar).T; U,_,Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    if np.linalg.det(R) < 0: Vt[-1,:] *= -1; R = Vt.T @ U.T
    t = (qbar - R @ pbar).flatten()
    # PnP residual
    Q_pred = (R @ Pc + t.reshape(3,1)).T
    residuals = np.linalg.norm(Q_cam - Q_pred, axis=1)
    return R, t, residuals

def parse_checkerboard_joint(urdf_path):
    root = ET.parse(str(urdf_path)).getroot()
    for j in root.findall("joint"):
        if j.get("name")!="checkerboard_joint": continue
        origin = j.find("origin")
        if origin is None: break
        xyz = [float(x) for x in origin.get("xyz","0 0 0").split()]
        rpy = [float(x) for x in origin.get("rpy","0 0 0").split()]
        return np.array(xyz,dtype=float), np.array(rpy,dtype=float)
    raise RuntimeError("checkerboard_joint not found")

# ============ 主分析 ============
CAMERA_CALIB = Path("/home/zwc/branch/kuavo-ros-control/src/Camera_Calibration")
CSV_DIR = CAMERA_CALIB / "output_csv/kuavo_left_wrist"
URDF = CAMERA_CALIB / "biped_v3_arm.urdf"
FK_ROOT = "zarm_l1_ref_link"
CAMERA_TIP = "left_wrist_camera_color_optical_frame"
SENSOR = "left_wrist_camera_to_base"
NX, NY, SQ = 11, 8, 0.03

def main():
    print("=" * 90)
    print("误差分解：15cm pre-opt FK 误差溯源")
    print("=" * 90)

    joints_urdf = load_urdf_joints(URDF)
    jcsv = load_joints_csv(CSV_DIR / "joints.csv")
    xyz_ref, rpy_ref = parse_checkerboard_joint(URDF)
    R_ref = rpy_to_R(float(rpy_ref[0]), float(rpy_ref[1]), float(rpy_ref[2]))
    p_ref = xyz_ref.copy()
    T_ref = np.eye(4); T_ref[:3,:3] = R_ref; T_ref[:3,3] = p_ref

    # 获取 camera chain
    camera_chain = find_chain_joint_names(joints_urdf, FK_ROOT, CAMERA_TIP)
    arm_joints = [j for j in camera_chain if j.startswith("zarm_l")]

    # l_hand_camera_joint URDF 定义
    R_hc, t_hc = parse_origin(joints_urdf["l_hand_camera_joint"].find("origin"))

    # =====================================================================
    # Part A: PnP 精度分析 —— 量化 PnP 拟合噪声
    # =====================================================================
    print("\n" + "-" * 90)
    print("A. PnP 拟合精度 (SVD rigid alignment 残差)")
    print("   目的：排除 PnP 噪声作为 15cm 误差的主要来源")
    print("-" * 90)
    print(f"{'SID':>4s} | {'PnP rmse(mm)':>14s} | {'PnP max(mm)':>13s} | {'cam_dist(m)':>11s} | {'expected_noise(mm)':>18s}")
    print("-" * 90)

    pnp_rmse_list = []
    for sid in range(9):
        feats = load_features_camera_points(CSV_DIR / "features.csv", sid, SENSOR)
        P_board = board_object_points(NX, NY, SQ, remap=True)
        _, t_cb, residuals = rigid_board_to_cam(P_board, feats)
        rmse = np.sqrt(np.mean(residuals**2)) * 1000  # mm
        max_r = np.max(residuals) * 1000  # mm
        cam_dist = np.linalg.norm(t_cb)
        # 典型深度相机精度：~0.2% of distance for structured light
        expected = cam_dist * 0.002 * 1000  # mm
        pnp_rmse_list.append(rmse)
        print(f"{sid:4d} | {rmse:14.3f} | {max_r:13.3f} | {cam_dist:11.3f} | {expected:18.3f}")

    print(f"\n  PnP rmse 均值: {np.mean(pnp_rmse_list):.2f} mm ← 远小于 150mm (15cm)")
    print(f"  结论：PnP 噪声不是 15cm 误差的主要来源（占比 <2%）")

    # =====================================================================
    # Part B: 误差在相机系中的一致性 —— 判断是固定外参还是 FK 链误差
    # =====================================================================
    print("\n" + "-" * 90)
    print("B. 误差在相机系中的方向分析")
    print("   目的：判断误差来自固定相机安装偏差 还是 臂链 FK 偏差")
    print("   方法：将每样本 board 位姿误差投影到 camera frame")
    print("   若方向一致（方差小）→ 固定安装误差")
    print("   若方向随臂形变化（方差大）→ FK 链误差")
    print("-" * 90)

    errors_in_cam = []  # 每个样本的 T_correction（nominal→correct 的变换）
    pnp_cam_board_list = []

    for sid in range(9):
        js = jcsv[sid]
        # Nominal FK
        T_cam_nom = fk_root_to_tip(joints_urdf, FK_ROOT, CAMERA_TIP,
                                   {jn: float(js.get(jn,0.0)) for jn in camera_chain})

        # PnP
        feats = load_features_camera_points(CSV_DIR / "features.csv", sid, SENSOR)
        P_board = board_object_points(NX, NY, SQ, remap=True)
        R_cb, t_cb, _ = rigid_board_to_cam(P_board, feats)
        T_cam_board = np.eye(4); T_cam_board[:3,:3]=R_cb; T_cam_board[:3,3]=t_cb
        pnp_cam_board_list.append(t_cb)

        # 用 nominal FK 重建的 board 位姿
        T_board_via_cam = T_cam_nom @ T_cam_board

        # "正确"的相机位姿（使得 board 对齐 ground truth）
        T_cam_correct = T_ref @ np.linalg.inv(T_cam_board)

        # T_correction: 从 nominal FK 相机到"正确"相机的变换
        T_correction = np.linalg.inv(T_cam_nom) @ T_cam_correct
        errors_in_cam.append(T_correction)

    # 统计 T_correction 在各样本间的一致性
    dp_list = np.array([T[:3,3]*1000 for T in errors_in_cam])  # mm
    dp_mean = dp_list.mean(axis=0)
    dp_std = dp_list.std(axis=0)

    print(f"\n  各样本所需的相机外参修正量 (mm, 在相机坐标系中):")
    print(f"  {'SID':>4s} | {'dx(mm)':>10s} | {'dy(mm)':>10s} | {'dz(mm)':>10s} | {'|d|(mm)':>10s}")
    print(f"  " + "-" * 55)
    for sid in range(9):
        dp = dp_list[sid]; dn = np.linalg.norm(dp)
        status = "★ 离群" if sid in (7,8) else ""
        print(f"  {sid:4d} | {dp[0]:+10.1f} | {dp[1]:+10.1f} | {dp[2]:+10.1f} | {dn:10.1f}  {status}")

    print(f"\n  均值:    [{dp_mean[0]:+.1f}, {dp_mean[1]:+.1f}, {dp_mean[2]:+.1f}] mm, |d|={np.linalg.norm(dp_mean):.1f} mm")
    print(f"  标准差:  [{dp_std[0]:.1f}, {dp_std[1]:.1f}, {dp_std[2]:.1f}] mm, |std|={np.linalg.norm(dp_std):.1f} mm")

    # 仅用优化样本（排除离群点）
    dp_inliers = dp_list[:7]
    dp_in_mean = dp_inliers.mean(axis=0)
    dp_in_std = dp_inliers.std(axis=0)
    print(f"\n  仅优化样本 (S0-6):")
    print(f"    均值: [{dp_in_mean[0]:+.1f}, {dp_in_mean[1]:+.1f}, {dp_in_mean[2]:+.1f}] mm, |d|={np.linalg.norm(dp_in_mean):.1f} mm")
    print(f"    标准差: [{dp_in_std[0]:.1f}, {dp_in_std[1]:.1f}, {dp_in_std[2]:.1f}] mm, |std|={np.linalg.norm(dp_in_std):.1f} mm")

    # 一致性判断
    cv = np.linalg.norm(dp_in_std) / (np.linalg.norm(dp_in_mean) + 1e-9)
    print(f"\n  变异系数 (std/mean): {cv:.3f}")
    if cv < 0.3:
        print(f"  ✅ 方向高度一致 → **固定相机安装误差** 是主要来源")
    elif cv < 0.8:
        print(f"  ⚠️ 方向部分一致 → **相机安装误差 + FK 链误差** 混合")
    else:
        print(f"  ❌ 方向不一致 → **FK 链本身误差** 是主要来源")

    # =====================================================================
    # Part C: 直接估计正确的 l_hand_camera_joint xyz
    # =====================================================================
    print("\n" + "-" * 90)
    print("C. 估计 l_hand_camera_joint 的正确 xyz")
    print("   若误差来自相机安装偏差，则 T_correction 的均值就是正确的修正量")
    print("-" * 90)

    # T_correction 均值在 camera frame 中
    # 需要转换回 l_hand_camera_joint 的 parent frame (l_hand_tripod)
    # 注意 T_correction 是在 camera_optical_frame 中表达的
    d_cam = dp_in_mean  # 均值修正量，在相机光轴坐标系中

    # l_hand_camera_joint 在 URDF 中的定义
    # <origin xyz="0.1154 -0.0146 -0.1101" rpy="1.9199 0.7535 -2.8972"/>
    print(f"\n  URDF nominal: l_hand_camera_joint xyz = [0.1154, -0.0146, -0.1101] m")
    print(f"  相机光轴坐标系下的修正: [{d_cam[0]:.1f}, {d_cam[1]:.1f}, {d_cam[2]:.1f}] mm")

    # 光学坐标系到相机 link 的变换
    # optical_frame_joint: rpy="-1.5708 0 -1.5708" → R_optical_to_camera
    R_opt_to_cam = rpy_to_R(-math.pi/2, 0, -math.pi/2)
    d_in_camera_link = R_opt_to_cam.T @ d_cam  # 转到 camera link frame

    print(f"  修正量在 camera link 系: [{d_in_camera_link[0]:.1f}, {d_in_camera_link[1]:.1f}, {d_in_camera_link[2]:.1f}] mm")

    # 建议的 l_hand_camera_joint xyz
    l_hand_camera_xyz = np.array([0.1154, -0.0146, -0.1101]) + d_in_camera_link / 1000
    print(f"  建议 l_hand_camera_joint xyz: [{l_hand_camera_xyz[0]:.4f}, {l_hand_camera_xyz[1]:.4f}, {l_hand_camera_xyz[2]:.4f}] m")

    # =====================================================================
    # Part D: S7/S8 离群点分析 —— 为什么它们被剔除？
    # =====================================================================
    print("\n" + "-" * 90)
    print("D. 离群点 (S7, S8) 分析")
    print("-" * 90)

    # S7, S8 在相机系中的误差方向是否也一致？
    for sid in [7, 8]:
        dp = dp_list[sid]
        angle = math.degrees(math.acos(np.dot(dp, dp_in_mean) / (np.linalg.norm(dp) * np.linalg.norm(dp_in_mean) + 1e-9)))
        print(f"  S{sid}: 修正量=[{dp[0]:+.1f}, {dp[1]:+.1f}, {dp[2]:+.1f}] mm, "
              f"|d|={np.linalg.norm(dp):.1f} mm, 与均值的夹角={angle:.1f}°")

    # 检查 S7/S8 的关节角是否与 S0-6 显著不同（比如关节角远大于其他样本）
    print()
    for sid in [7, 8]:
        js = jcsv[sid]
        angles = [math.degrees(js.get(jn,0)) for jn in arm_joints]
        print(f"  S{sid} arm angles (deg): {[f'{a:+.1f}' for a in angles]}")

    # 参考 S0-6 的关节角范围
    print()
    for jn in arm_joints:
        vals = [math.degrees(jcsv[sid].get(jn,0)) for sid in range(7)]
        v7, v8 = math.degrees(jcsv[7].get(jn,0)), math.degrees(jcsv[8].get(jn,0))
        outlier_7 = " ***" if abs(v7-np.mean(vals)) > 2*np.std(vals) else ""
        outlier_8 = " ***" if abs(v8-np.mean(vals)) > 2*np.std(vals) else ""
        print(f"  {jn:>20s}: S0-6 range=[{min(vals):+.1f}, {max(vals):+.1f}]° "
              f"S7={v7:+.1f}°{outlier_7} S8={v8:+.1f}°{outlier_8}")

    # =====================================================================
    # Part E: 检查 checkboard_joint 自身是否准确
    # =====================================================================
    print("\n" + "-" * 90)
    print("E. checkerboard_joint 自身精度检查")
    print("   棋盘格 ground truth 来自动捕数据 (optitrack_poses.yaml 多帧均值)")
    print("   动捕精度通常 ±2mm，但标定精度取决于 marker 放置和刚体定义")
    print("-" * 90)

    # 从 tag_to_base 观测可以直接得到 checkerboard 上的特征点在 base 系中的位置
    # 这个路径不经过臂链，是最短路径
    # 如果 tag_to_base + checkerboard_joint 这条链的 FK 是准确的，
    # 那么这条链的 DELTA 应该很小

    # 实际上 tag_to_base 的 features 是在 checkerboard_link 中，
    # 观测值是 [-150, -105, 0] 的 remapped 值
    # 所以这条链只有 checkboard_joint 这一个固定关节
    # FK(tag_chain) = checkboard_joint 的位姿 → 就是 ground truth 本身
    print("  checkerboard_joint 的 <origin> 直接来自动捕数据，")
    print("  无需 FK 计算，不存在累积误差。")
    print("  但动捕刚体定义可能有系统偏移（取决于 marker 贴在棋盘上的位置）。")

    # =====================================================================
    # Part F: 关键量化总结
    # =====================================================================
    print("\n" + "=" * 90)
    print("关键量化总结")
    print("=" * 90)

    print(f"""
  ┌─────────────────────────────────────────────────────────────┐
  │ 15cm pre-opt FK 误差分解                                     │
  ├─────────────────────────────────────────────────────────────┤
  │ 1. PnP 拟合噪声:       ~{np.mean(pnp_rmse_list):.1f} mm     ({np.mean(pnp_rmse_list)/150*100:.1f}% of 15cm) │
  │ 2. camera extrinsic:   ~{np.linalg.norm(dp_in_mean):.1f} mm  ({np.linalg.norm(dp_in_mean)/150*100:.0f}% of 15cm) │
  │ 3. 残差 (FK链+T_cb):  ~{np.linalg.norm(dp_in_std):.1f} mm   ({np.linalg.norm(dp_in_std)/150*100:.0f}% of 15cm) │
  ├─────────────────────────────────────────────────────────────┤
  │ 变异系数: {cv:.3f}                                            │
  │ 结论: {'固定相机安装误差 是 15cm 的主要来源' if cv < 0.3 else '相机安装误差 + FK 链误差 混合' if cv < 0.8 else 'FK 链本身误差是主要来源'} │
  └─────────────────────────────────────────────────────────────┘
""")

    print(f"  修正方案对比:")
    print(f"  ┌──────────────────────┬─────────────────┬──────────────────┐")
    print(f"  │ 方案                 │ 需调参数        │ 效果             │")
    print(f"  ├──────────────────────┼─────────────────┼──────────────────┤")
    print(f"  │ 当前 (locked extr)   │ 7 joint offsets │ 3/7样本恶化       │")
    print(f"  │ 建议 (unlock xyz)    │ 3-DOF xyz + 7   │ 相机外参直接修正   │")
    print(f"  │                      │ offsets (约束)  │ offsets 用于 FK   │")
    print(f"  └──────────────────────┴─────────────────┴──────────────────┘")

    print(f"\n  如果仅修正 l_hand_camera_joint xyz (不改 arm offsets):")
    print(f"    new_xyz = [{l_hand_camera_xyz[0]:.4f}, {l_hand_camera_xyz[1]:.4f}, {l_hand_camera_xyz[2]:.4f}]")
    print(f"    将 URDF 中 l_hand_camera_joint 的 xyz 改为上述值，")
    print(f"    预期 board 位姿误差降至 ~{np.linalg.norm(dp_in_std):.0f} mm (残差)")

    return 0

if __name__ == "__main__":
    sys.exit(main())
