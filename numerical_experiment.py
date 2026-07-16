#!/usr/bin/env python3
"""
数值对照实验：量化验证 locked camera extrinsics → Ceres 用 joint offsets 错误补偿

实验设计：
1. 取每个样本的关节角度 + 相机特征点
2. 用 SVD rigid alignment (PnP等效) 计算相机坐标系下的棋盘位姿
3. FK(zarm_l1_ref_link → left_wrist_camera_color_optical_frame) 计算相机在base系位姿
4. 棋盘在base系位姿 = FK(base→camera) * PnP(camera→board)
5. 对比URDF checkerboard_joint(ground truth)，计算误差
6. 对比三组offsets下的误差：nominal, calibrated, hypothetical(有3DOF相机外参修正)
"""

import csv
import math
import sys
from pathlib import Path
import numpy as np
import xml.etree.ElementTree as ET

# ============ 配置 ============
CAMERA_CALIB_ROOT = Path("/home/zwc/branch/kuavo-ros-control/src/Camera_Calibration")
CSV_DIR = CAMERA_CALIB_ROOT / "output_csv/kuavo_left_wrist"
NOMINAL_URDF = CAMERA_CALIB_ROOT / "biped_v3_arm.urdf"
CALIB_YAML = CAMERA_CALIB_ROOT / "output/kuavo_left_wrist/calibration.yaml"

FK_ROOT = "zarm_l1_ref_link"
CAMERA_TIP = "left_wrist_camera_color_optical_frame"
SENSOR_NAME = "left_wrist_camera_to_base"

# 棋盘格参数
POINTS_X, POINTS_Y = 11, 8
SQUARE_SIZE = 0.03

# ============ URDF FK 实现（与 plot_board_error_from_csv.py 一致）============

def rpy_to_R(r, p, y):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    return Rz @ Ry @ Rx

def parse_origin(el):
    if el is None:
        return np.eye(3), np.zeros(3)
    xyz = [float(x) for x in el.get("xyz", "0 0 0").split()]
    rpy = [float(x) for x in el.get("rpy", "0 0 0").split()]
    return rpy_to_R(rpy[0], rpy[1], rpy[2]), np.array(xyz, dtype=float)

def parse_axis(joint_el):
    ax = joint_el.find("axis")
    if ax is None:
        return np.array([0.0, 0.0, 1.0], dtype=float)
    v = np.array([float(x) for x in ax.get("xyz", "0 0 1").split()], dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else np.array([0.0, 0.0, 1.0], dtype=float)

def rodrigues(axis, angle):
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    C = 1.0 - c
    return np.array([
        [c + x*x*C, x*y*C - z*s, x*z*C + y*s],
        [y*x*C + z*s, c + y*y*C, y*z*C - x*s],
        [z*x*C - y*s, z*y*C + x*s, c + z*z*C],
    ], dtype=float)

def joint_T(q, joint_el):
    jtype = joint_el.get("type", "fixed")
    R0, t0 = parse_origin(joint_el.find("origin"))
    T = np.eye(4)
    T[:3, :3] = R0
    T[:3, 3] = t0
    if jtype == "fixed":
        return T
    if jtype != "revolute":
        raise RuntimeError(f"unsupported joint type: {jtype}")
    Rq = rodrigues(parse_axis(joint_el), float(q))
    Tq = np.eye(4)
    Tq[:3, :3] = Rq
    return T @ Tq

def load_urdf_joints(urdf_path):
    root = ET.parse(str(urdf_path)).getroot()
    joints = {}
    for j in root.findall("joint"):
        name = j.get("name")
        if name:
            joints[name] = j
    return joints

def find_chain_joint_names(joints, root_link, tip_link):
    joint_by_child = {}
    parent_of_child = {}
    for jname, jel in joints.items():
        par = jel.find("parent")
        chi = jel.find("child")
        if par is None or chi is None:
            continue
        pl, cl = par.get("link"), chi.get("link")
        if not pl or not cl:
            continue
        joint_by_child[cl] = jname
        parent_of_child[cl] = pl
    chain_rev = []
    cur = tip_link
    while cur != root_link:
        if cur not in joint_by_child:
            raise RuntimeError(f"cannot walk from {tip_link} to {root_link}, stuck at {cur}")
        chain_rev.append(joint_by_child[cur])
        cur = parent_of_child[cur]
    chain_rev.reverse()
    return chain_rev

def fk_base_to_tip(joints, chain, q_by_joint):
    T = np.eye(4)
    for jn in chain:
        T = T @ joint_T(float(q_by_joint.get(jn, 0.0)), joints[jn])
    return T

def fk_root_to_tip_transform(joints, fk_root, tip_link, q_by_joint):
    try:
        chain = find_chain_joint_names(joints, fk_root, tip_link)
        return fk_base_to_tip(joints, chain, q_by_joint)
    except RuntimeError:
        # 桥接路径 (zarm_l1_ref_link 通过 waist_yaw_link)
        chain_w_t = find_chain_joint_names(joints, "waist_yaw_link", tip_link)
        chain_w_z = find_chain_joint_names(joints, "waist_yaw_link", fk_root)
        t_w_t = fk_base_to_tip(joints, chain_w_t, q_by_joint)
        t_w_z = fk_base_to_tip(joints, chain_w_z, q_by_joint)
        return np.linalg.inv(t_w_z) @ t_w_t

def load_offsets_yaml(path):
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, rest = line.partition(":")
        k = k.strip()
        try:
            v = float(rest.strip())
        except ValueError:
            continue
        if "_joint" in k or k == "waist_yaw_joint":
            out[k] = v
    return out

# ============ 数据加载 ============

def load_joints_csv(path):
    by_sample = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            sid = int(row["sample_id"])
            by_sample.setdefault(sid, {})[row["joint_name"].strip()] = float(row["position"])
    return by_sample

def load_features_camera_points(path, sample_id, sensor_name):
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if int(row["sample_id"]) != int(sample_id):
                continue
            if row["sensor_name"].strip() != sensor_name:
                continue
            idx = int(row["feature_idx"])
            p = np.array([float(row["x"]), float(row["y"]), float(row["z"])], dtype=float)
            rows.append((idx, p))
    rows.sort(key=lambda x: x[0])
    return np.stack([x[1] for x in rows], axis=0)

def board_object_points(nx, ny, square, remap_to_center=True):
    pts = np.array([[(i % nx) * square, (i // nx) * square, 0.0] for i in range(nx * ny)], dtype=float)
    if remap_to_center:
        pts[:, 0] -= ((nx - 1) * square * 0.5)
        pts[:, 1] -= ((ny - 1) * square * 0.5)
    return pts

def rigid_board_to_cam(P_board, Q_cam):
    """SVD rigid alignment: P_board → Q_cam, returns R_cb, t_cb (board-to-camera)"""
    Pc, Qc = P_board.T, Q_cam.T
    pbar = Pc.mean(axis=1, keepdims=True)
    qbar = Qc.mean(axis=1, keepdims=True)
    H = (Pc - pbar) @ (Qc - qbar).T
    U, _, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = Vt.T @ U.T
    t = (qbar - R @ pbar).flatten()
    return R, t

def parse_checkerboard_joint(urdf_path):
    root = ET.parse(str(urdf_path)).getroot()
    for j in root.findall("joint"):
        if j.get("name") != "checkerboard_joint":
            continue
        origin = j.find("origin")
        if origin is None:
            break
        xyz = [float(x) for x in origin.get("xyz", "0 0 0").split()]
        rpy = [float(x) for x in origin.get("rpy", "0 0 0").split()]
        return np.array(xyz, dtype=float), np.array(rpy, dtype=float)
    raise RuntimeError(f"checkerboard_joint not found in {urdf_path}")

def rotation_angle_deg(R_est, R_ref):
    R_err = R_ref.T @ R_est
    c = (np.trace(R_err) - 1.0) * 0.5
    c = float(np.clip(c, -1.0, 1.0))
    return math.degrees(math.acos(c))

# ============ 主实验 ============

def compute_camera_fk(joints, fk_root, camera_tip, js, offsets_dict):
    """Compute FK(base->camera) with given joint values + offsets"""
    try:
        chain = find_chain_joint_names(joints, fk_root, camera_tip)
    except RuntimeError:
        chain = list(dict.fromkeys(
            find_chain_joint_names(joints, "waist_yaw_link", camera_tip) +
            find_chain_joint_names(joints, "waist_yaw_link", fk_root)
        ))
    q_by_joint = {jn: float(js.get(jn, 0.0)) + float(offsets_dict.get(jn, 0.0))
                  for jn in chain}
    return fk_root_to_tip_transform(joints, fk_root, camera_tip, q_by_joint)

def compute_board_in_base(joints, fk_root, camera_tip, js, offsets_dict,
                           csv_dir, sample_id, sensor_name, nx, ny, square):
    """Full pipeline: FK(base->cam) * PnP(cam->board) = board pose in base"""
    T_base_cam = compute_camera_fk(joints, fk_root, camera_tip, js, offsets_dict)

    feats = load_features_camera_points(csv_dir / "features.csv", sample_id, sensor_name)
    P_board = board_object_points(nx, ny, square, remap_to_center=True)
    R_cb, t_cb = rigid_board_to_cam(P_board, feats)
    T_cam_board = np.eye(4)
    T_cam_board[:3, :3] = R_cb
    T_cam_board[:3, 3] = t_cb

    T_base_board = T_base_cam @ T_cam_board
    return T_base_board, T_base_cam, T_cam_board


def main():
    print("=" * 80)
    print("数值对照实验：locked camera extrinsics → offsets 错误补偿")
    print("=" * 80)

    # 加载数据
    joints_urdf = load_urdf_joints(NOMINAL_URDF)
    jcsv = load_joints_csv(CSV_DIR / "joints.csv")
    calib_offsets = load_offsets_yaml(CALIB_YAML) if CALIB_YAML.is_file() else {}
    xyz_ref, rpy_ref = parse_checkerboard_joint(NOMINAL_URDF)
    R_ref = rpy_to_R(float(rpy_ref[0]), float(rpy_ref[1]), float(rpy_ref[2]))
    p_ref = xyz_ref.copy()

    # 测试样本（optimization_used_sample_ids）
    used_ids = [0, 1, 2, 3, 4, 5, 6]
    # 也看看被离群点剔除的样本
    outlier_ids = [7, 8]
    all_ids = used_ids + outlier_ids

    # 获取 FK chain 和 l_hand_camera_joint 的 URDF 定义
    try:
        camera_chain = find_chain_joint_names(joints_urdf, FK_ROOT, CAMERA_TIP)
    except RuntimeError:
        camera_chain = list(dict.fromkeys(
            find_chain_joint_names(joints_urdf, "waist_yaw_link", CAMERA_TIP) +
            find_chain_joint_names(joints_urdf, "waist_yaw_link", FK_ROOT)
        ))

    # l_hand_camera_joint 的 URDF origin
    hand_cam_joint = joints_urdf.get("l_hand_camera_joint")
    if hand_cam_joint is not None:
        R_hc, t_hc = parse_origin(hand_cam_joint.find("origin"))
        print(f"\nl_hand_camera_joint URDF origin:")
        print(f"  t = [{t_hc[0]:.4f}, {t_hc[1]:.4f}, {t_hc[2]:.4f}] m")
    else:
        R_hc, t_hc = np.eye(3), np.zeros(3)
        print("\nWARNING: l_hand_camera_joint not found in URDF")

    # 提取 arm joints only
    arm_joints = [j for j in camera_chain if j.startswith("zarm_l")]

    print(f"\nFK chain ({FK_ROOT} → {CAMERA_TIP}):")
    print(f"  Joints: {' → '.join(camera_chain)}")
    print(f"  Arm joints: {arm_joints}")

    print(f"\n棋盘 ground truth (checkerboard_joint in URDF):")
    print(f"  p_ref = [{p_ref[0]:.4f}, {p_ref[1]:.4f}, {p_ref[2]:.4f}] m")

    print(f"\n标定 offsets (calibration.yaml):")
    for jn in arm_joints:
        off_rad = calib_offsets.get(jn, 0.0)
        print(f"  {jn}: {off_rad:.6f} rad ({math.degrees(off_rad):.2f} deg)")

    # ============ 实验 1：逐样本对比 nominal vs calibrated ============
    print("\n" + "=" * 80)
    print("实验 1：逐样本 FK 误差对比（nominal vs calibrated offsets）")
    print("=" * 80)

    results = []
    for sid in all_ids:
        js = jcsv[sid]

        # Nominal
        T_board_nom, T_cam_nom, T_cam_board = compute_board_in_base(
            joints_urdf, FK_ROOT, CAMERA_TIP, js, {}, CSV_DIR, sid, SENSOR_NAME,
            POINTS_X, POINTS_Y, SQUARE_SIZE)

        # Calibrated
        T_board_cal, T_cam_cal, _ = compute_board_in_base(
            joints_urdf, FK_ROOT, CAMERA_TIP, js, calib_offsets, CSV_DIR, sid, SENSOR_NAME,
            POINTS_X, POINTS_Y, SQUARE_SIZE)

        # Errors
        p_nom = T_board_nom[:3, 3]
        p_cal = T_board_cal[:3, 3]
        e_pos_nom = np.linalg.norm(p_nom - p_ref)
        e_pos_cal = np.linalg.norm(p_cal - p_ref)
        e_rot_nom = rotation_angle_deg(T_board_nom[:3, :3], R_ref)
        e_rot_cal = rotation_angle_deg(T_board_cal[:3, :3], R_ref)

        # Camera position change
        p_cam_nom = T_cam_nom[:3, 3]
        p_cam_cal = T_cam_cal[:3, 3]
        cam_delta = np.linalg.norm(p_cam_cal - p_cam_nom)

        results.append({
            'sid': sid,
            'p_nom': p_nom, 'p_cal': p_cal,
            'p_cam_nom': p_cam_nom, 'p_cam_cal': p_cam_cal,
            'e_pos_nom': e_pos_nom, 'e_pos_cal': e_pos_cal,
            'e_rot_nom': e_rot_nom, 'e_rot_cal': e_rot_cal,
            'cam_delta': cam_delta,
            'outlier': sid in outlier_ids
        })

    # 打印汇总表
    print(f"\n{'SID':>4s} | {'pos_nom(m)':>10s} | {'pos_cal(m)':>10s} | {'Δpos(m)':>10s} | "
          f"{'rot_nom(°)':>10s} | {'rot_cal(°)':>10s} | {'Δrot(°)':>10s} | "
          f"{'cam_shift(m)':>12s} | {'status':>8s}")
    print("-" * 105)

    for r in results:
        status = "OUTLIER" if r['outlier'] else "OK"
        dpos = r['e_pos_nom'] - r['e_pos_cal']
        drot = r['e_rot_nom'] - r['e_rot_cal']
        print(f"{r['sid']:4d} | {r['e_pos_nom']:10.6f} | {r['e_pos_cal']:10.6f} | {dpos:+10.6f} | "
              f"{r['e_rot_nom']:10.4f} | {r['e_rot_cal']:10.4f} | {drot:+10.4f} | "
              f"{r['cam_delta']:12.6f} | {status:>8s}")

    # 均值
    used_results = [r for r in results if not r['outlier']]
    e_pos_nom_mean = np.mean([r['e_pos_nom'] for r in used_results])
    e_pos_cal_mean = np.mean([r['e_pos_cal'] for r in used_results])
    e_rot_nom_mean = np.mean([r['e_rot_nom'] for r in used_results])
    e_rot_cal_mean = np.mean([r['e_rot_cal'] for r in used_results])
    cam_shift_mean = np.mean([r['cam_delta'] for r in used_results])

    print(f"\n{'mean':>4s} | {e_pos_nom_mean:10.6f} | {e_pos_cal_mean:10.6f} | "
          f"{e_pos_nom_mean-e_pos_cal_mean:+10.6f} | "
          f"{e_rot_nom_mean:10.4f} | {e_rot_cal_mean:10.4f} | "
          f"{e_rot_nom_mean-e_rot_cal_mean:+10.4f} | "
          f"{cam_shift_mean:12.6f} |")

    # ============ 实验 2：Jacobian 分析——offset 对末端位姿的敏感度 ============
    print("\n" + "=" * 80)
    print("实验 2：joint offset → 末端(camera tip) 位置 Jacobian")
    print("目的：量化每个关节 offset 对相机位置的放大效应")
    print("=" * 80)

    # 取第一个样本的关节角作为参考位形
    js_ref = jcsv[0]

    # 对每个 arm joint，计算 +0.01 rad offset 导致的相机位置变化
    delta = 0.01  # rad
    T_base_cam_nom = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js_ref, {})
    p_cam_nom = T_base_cam_nom[:3, 3]

    print(f"\n参考位形 (sample 0) 下，每 +0.01 rad offset 导致的相机位置偏移:")
    print(f"{'Joint':>20s} | {'dp_x(mm)':>10s} | {'dp_y(mm)':>10s} | {'dp_z(mm)':>10s} | "
          f"{'|dp|(mm)':>10s} | {'sens(mm/rad)':>12s}")
    print("-" * 85)

    sensitivities = {}
    for jn in arm_joints:
        offsets_test = {jn: delta}
        T_base_cam_test = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js_ref, offsets_test)
        p_cam_test = T_base_cam_test[:3, 3]
        dp = (p_cam_test - p_cam_nom) * 1000  # mm
        dp_norm = np.linalg.norm(dp)
        sens = dp_norm / delta  # mm/rad
        sensitivities[jn] = sens
        print(f"{jn:>20s} | {dp[0]:+10.3f} | {dp[1]:+10.3f} | {dp[2]:+10.3f} | "
              f"{dp_norm:10.3f} | {sens:12.1f}")

    # ============ 实验 3：offset 综合效应——将 offset 换算成等效的相机 3DOF 平移 ============
    print("\n" + "=" * 80)
    print("实验 3：标定 offsets 综合效应")
    print("目的：量化整个 offset 向量对相机位姿的影响")
    print("=" * 80)

    for sid in used_ids[:3]:  # 前3个样本
        js = jcsv[sid]
        T_cam_nom = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, {})
        T_cam_cal = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, calib_offsets)

        p_nom = T_cam_nom[:3, 3]
        p_cal = T_cam_cal[:3, 3]
        dp_full = (p_cal - p_nom) * 1000  # mm
        dp_full_norm = np.linalg.norm(dp_full)

        # 用 linearized Jacobian 估算（只考虑 0.01 rad 的敏感度 * 实际 offset/0.01）
        dp_est = np.zeros(3)
        for jn in arm_joints:
            off = calib_offsets.get(jn, 0.0)
            offsets_test = {jn: delta}
            T_test = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, offsets_test)
            dp_est += (T_test[:3, 3] - T_cam_nom[:3, 3]) * (off / delta) * 1000

        print(f"\nS{sid}: offset 导致相机位置偏移:")
        print(f"  实际 FK: dp = [{dp_full[0]:+.1f}, {dp_full[1]:+.1f}, {dp_full[2]:+.1f}] mm, |dp| = {dp_full_norm:.1f} mm")
        print(f"  线性近似: dp = [{dp_est[0]:+.1f}, {dp_est[1]:+.1f}, {dp_est[2]:+.1f}] mm, |dp| = {np.linalg.norm(dp_est):.1f} mm")

        # 输出每个关节单独贡献
        print(f"  各关节贡献 (mm):")
        for jn in arm_joints:
            off = calib_offsets.get(jn, 0.0)
            if abs(off) < 1e-6:
                continue
            offsets_test = {jn: delta}
            T_test = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, offsets_test)
            dp_j = (T_test[:3, 3] - T_cam_nom[:3, 3]) * (off / delta) * 1000
            dp_j_norm = np.linalg.norm(dp_j)
            print(f"    {jn}: off={math.degrees(off):+.1f}°, dp=[{dp_j[0]:+.1f}, {dp_j[1]:+.1f}, {dp_j[2]:+.1f}] mm, |dp|={dp_j_norm:.1f} mm")

    # ============ 实验 4：关键洞察——相机外参补偿 vs offset 补偿 ============
    print("\n" + "=" * 80)
    print("实验 4：关键对比——用相机外参 3DOF 修正 vs 用 joint offsets 修正")
    print("=" * 80)

    # 取一个具体样本（如 S0）深入分析
    sid = 0
    js = jcsv[sid]
    T_cam_nom = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, {})
    T_cam_cal = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, calib_offsets)

    # PnP 计算 camera→board
    feats = load_features_camera_points(CSV_DIR / "features.csv", sid, SENSOR_NAME)
    P_board = board_object_points(POINTS_X, POINTS_Y, SQUARE_SIZE, remap_to_center=True)
    R_cb, t_cb = rigid_board_to_cam(P_board, feats)
    T_cam_board = np.eye(4)
    T_cam_board[:3, :3] = R_cb
    T_cam_board[:3, 3] = t_cb

    # 棋盘在base系：
    T_board_nom = T_cam_nom @ T_cam_board
    T_board_cal = T_cam_cal @ T_cam_board

    e_pos_nom = np.linalg.norm(T_board_nom[:3, 3] - p_ref) * 1000
    e_pos_cal = np.linalg.norm(T_board_cal[:3, 3] - p_ref) * 1000

    print(f"\n样本 S{sid} 深入分析:")
    print(f"  PnP camera→board: t = [{t_cb[0]:.3f}, {t_cb[1]:.3f}, {t_cb[2]:.3f}] m")

    p_cam_nom = T_cam_nom[:3, 3]
    p_cam_cal = T_cam_cal[:3, 3]
    print(f"  FK base→camera (nominal): p = [{p_cam_nom[0]:.4f}, {p_cam_nom[1]:.4f}, {p_cam_nom[2]:.4f}] m")
    print(f"  FK base→camera (calibrated): p = [{p_cam_cal[0]:.4f}, {p_cam_cal[1]:.4f}, {p_cam_cal[2]:.4f}] m")
    print(f"  Camera shift from offsets: {np.linalg.norm(p_cam_cal - p_cam_nom)*1000:.1f} mm")

    p_board_nom = T_board_nom[:3, 3]
    p_board_cal = T_board_cal[:3, 3]
    print(f"  Board in base (nominal): p = [{p_board_nom[0]:.4f}, {p_board_nom[1]:.4f}, {p_board_nom[2]:.4f}] m")
    print(f"  Board in base (calibrated): p = [{p_board_cal[0]:.4f}, {p_board_cal[1]:.4f}, {p_board_cal[2]:.4f}] m")
    print(f"  Board ground truth: p = [{p_ref[0]:.4f}, {p_ref[1]:.4f}, {p_ref[2]:.4f}] m")
    print(f"  Board error (nominal): {e_pos_nom:.1f} mm")
    print(f"  Board error (calibrated): {e_pos_cal:.1f} mm")

    # 假设：如果相机外参可以自由调整（修正为 xyz 正确值）
    # 那么我们可以直接用 PnP 算出的相机位姿反推相机应该在的位置
    # 但实际上这里 PnP 给出的是 camera→board，我们只能观测到 board error
    # 关键论证：offsets 改变了相机位置，但正确的做法应该是改变 l_hand_camera_joint 的 xyz

    # 计算 offset 在 l_hand_camera_joint 层面的等效 3DOF 平移
    dp_cam = p_cam_cal - p_cam_nom
    print(f"\n  关键发现:")
    print(f"  offset 将相机移动了 {np.linalg.norm(dp_cam)*1000:.1f} mm")
    print(f"  这个移动在 {len(arm_joints)}-DOF 关节空间完成，但在末端只有 3-DOF 效果")
    print(f"  如果直接调整 l_hand_camera_joint 的 xyz 3DOF，可以达到同样的末端位姿修正")
    print(f"  区别在于：")
    print(f"    1) joint offsets 会影响整条臂上所有 link 的位姿（不只是相机）")
    print(f"    2) 但棋盘标定只观测相机→棋盘，不直接观测各 link")
    print(f"    3) Ceres 将残差通过 Jacobian 反向传播到 7 个 joint offsets")
    print(f"    4) 由于 7-DOF 对末端位姿高度耦合，会产生多组 offset 组合达到相似的末端效果")
    print(f"    5) 但被 outrageous error block (position_scale=0.1) 约束的 l_hand_camera_joint 无法调整")

    # ============ 实验 5：假设验证——如果 l_hand_camera_joint 3DOF 自由 ============
    print("\n" + "=" * 80)
    print("实验 5：假设验证——如果解锁 l_hand_camera_joint 的 x/y/z 3DOF")
    print("=" * 80)

    # 模拟：对每个样本，找一个 l_hand_camera_joint xyz offset 使得棋盘误差最小
    # 实际上这就是：正确的 T_base_cam 应该使得 T_base_cam * T_cam_board = T_board_ref
    # 所以 T_base_cam_correct = T_board_ref * inv(T_cam_board)
    # 然后 camera joint offset = inv(T_base_cam_nom) * T_base_cam_correct （相对于nominal的变换）

    T_board_ref = np.eye(4)
    T_board_ref[:3, :3] = R_ref
    T_board_ref[:3, 3] = p_ref

    print(f"\n{'SID':>4s} | {'cam_shift_via_offset(mm)':>24s} | {'cam_shift_if_free_joint(mm)':>26s} | {'equiv_joint_xyz(mm)':>24s}")
    print("-" * 90)

    for sid in used_ids:
        js = jcsv[sid]
        T_cam_nom_s = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, {})
        T_cam_cal_s = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, calib_offsets)

        feats_s = load_features_camera_points(CSV_DIR / "features.csv", sid, SENSOR_NAME)
        P_board_s = board_object_points(POINTS_X, POINTS_Y, SQUARE_SIZE, remap_to_center=True)
        R_cb_s, t_cb_s = rigid_board_to_cam(P_board_s, feats_s)
        T_cam_board_s = np.eye(4)
        T_cam_board_s[:3, :3] = R_cb_s
        T_cam_board_s[:3, 3] = t_cb_s

        # "正确"的相机位姿（使得 board 恰好对齐 ground truth）
        T_cam_correct = T_board_ref @ np.linalg.inv(T_cam_board_s)

        # 如果相机外参自由，应该施加的修正量
        T_correction = np.linalg.inv(T_cam_nom_s) @ T_cam_correct
        dp_correction = T_correction[:3, 3] * 1000  # mm

        # 当前 offset 造成的相机位姿变化
        dp_via_offset = (T_cam_cal_s[:3, 3] - T_cam_nom_s[:3, 3]) * 1000  # mm

        print(f"{sid:4d} | {np.linalg.norm(dp_via_offset):24.1f} | "
              f"{np.linalg.norm(dp_correction):26.1f} | "
              f"[{dp_correction[0]:+6.1f}, {dp_correction[1]:+6.1f}, {dp_correction[2]:+6.1f}]")

    # ============ 最终量化结论 ============
    print("\n" + "=" * 80)
    print("最终量化结论")
    print("=" * 80)

    # 计算 offset 对末端位置的 RMS 效应
    dp_norms = []
    dp_correct_norms = []
    for sid in used_ids:
        js = jcsv[sid]
        T_cam_nom_s = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, {})
        T_cam_cal_s = compute_camera_fk(joints_urdf, FK_ROOT, CAMERA_TIP, js, calib_offsets)
        dp_norms.append(np.linalg.norm(T_cam_cal_s[:3, 3] - T_cam_nom_s[:3, 3]) * 1000)

        feats_s = load_features_camera_points(CSV_DIR / "features.csv", sid, SENSOR_NAME)
        P_board_s = board_object_points(POINTS_X, POINTS_Y, SQUARE_SIZE, remap_to_center=True)
        R_cb_s, t_cb_s = rigid_board_to_cam(P_board_s, feats_s)
        T_cam_board_s = np.eye(4)
        T_cam_board_s[:3, :3] = R_cb_s
        T_cam_board_s[:3, 3] = t_cb_s
        T_cam_correct = T_board_ref @ np.linalg.inv(T_cam_board_s)
        T_correction = np.linalg.inv(T_cam_nom_s) @ T_cam_correct
        dp_correct_norms.append(np.linalg.norm(T_correction[:3, 3]) * 1000)

    print(f"\n1. offset 导致的相机位置偏移（7 个测试样本）:")
    print(f"   均值: {np.mean(dp_norms):.1f} mm, 最大: {np.max(dp_norms):.1f} mm, 最小: {np.min(dp_norms):.1f} mm")

    print(f"\n2. 如果 l_hand_camera_joint xyz 自由，需要的相机外参修正量:")
    print(f"   均值: {np.mean(dp_correct_norms):.1f} mm, 最大: {np.max(dp_correct_norms):.1f} mm, 最小: {np.min(dp_correct_norms):.1f} mm")

    # 计算 arm link 总长度（从 zarm_l1 到 wrist 末端）
    # 这决定了 offset 被放大的程度
    print(f"\n3. 7-DOF 臂各关节到末端的近似距离（决定 offset 放大倍数）:")
    # 取 FK chain 中各段的大致长度
    total_length = 0
    chain_for_fk = find_chain_joint_names(joints_urdf, "waist_yaw_link", CAMERA_TIP)
    for jn in chain_for_fk:
        jel = joints_urdf.get(jn)
        if jel is not None:
            origin = jel.find("origin")
            if origin is not None:
                xyz = [float(x) for x in origin.get("xyz", "0 0 0").split()]
                seg_len = np.linalg.norm(xyz)
                if seg_len > 0.001 and jn != "zarm_l1_ref_joint":
                    total_length += seg_len

    # 手动计算总 arm 长度
    arm_joint_offsets_urdf = {
        "zarm_l2_joint": [0, 0, 0],
        "zarm_l3_joint": [0, 0, 0],
        "zarm_l4_joint": [0.02, 0, -0.2837],
        "zarm_l5_joint": [-0.02, 0, -0.1201],
        "zarm_l6_joint": [0, 0, -0.114],
        "zarm_l7_joint": [-0.0002, 0, -0.021],
        "zarm_l7_end_effector_joint": [0, -0.03, -0.17],
        "l_hand_tripod_joint": [0, 0, 0],
        "l_hand_camera_joint": [0.1154, -0.0146, -0.1101],
        "left_wrist_camera_color_optical_frame_joint": [0, 0, 0],
    }
    # 从 zarm_l4 之后的 segment 长度累积（远端关节对末端影响更大）
    # dist_to_tip: the distance from each joint to the camera tip, accumulated along chain

    # 简化：累积 arm 链路长度
    # 从 zarm_l1 开始遍历 chain
    chain_z = find_chain_joint_names(joints_urdf, FK_ROOT, CAMERA_TIP)
    cumulative_len = 0
    joint_dist_to_tip = {}
    # 反向累积
    chain_z_rev = list(reversed(chain_z))
    for jn in chain_z_rev:
        if jn in arm_joints:
            joint_dist_to_tip[jn] = cumulative_len
        jel = joints_urdf.get(jn)
        if jel is not None:
            origin = jel.find("origin")
            if origin is not None:
                xyz = [float(x) for x in origin.get("xyz", "0 0 0").split()]
                cumulative_len += np.linalg.norm(xyz)

    print(f"   总臂长（zarm_l1 → camera_optical_frame）: {cumulative_len:.3f} m")
    for jn in arm_joints:
        if jn in joint_dist_to_tip:
            d = joint_dist_to_tip[jn]
            off_rad = calib_offsets.get(jn, 0.0)
            # 近似放大：offset * distance
            linear_equiv = abs(off_rad) * d * 1000  # mm
            off_deg = math.degrees(off_rad)
            print(f"   {jn}: dist_to_tip={d:.3f}m, offset={off_deg:+.1f}°, "
                  f"等效~{linear_equiv:.0f}mm 末端位移 (offset×dist)")

    max_offset_deg = max(abs(math.degrees(calib_offsets.get(jn, 0.0))) for jn in arm_joints)
    max_offset_joint = max(arm_joints, key=lambda jn: abs(calib_offsets.get(jn, 0.0)))

    print(f"\n4. 核心发现:")
    print(f"   a) 最大单个 offset: {max_offset_joint} = {max_offset_deg:.1f}°")
    print(f"   b) offset 将相机移动了 ~{np.mean(dp_norms):.0f} mm（各样本均值）")
    print(f"   c) 若 l_hand_camera_joint 3DOF 自由，只需 ~{np.mean(dp_correct_norms):.0f} mm 平移修正")
    print(f"   d) 但 Ceres 被迫用 7-DOF 关节 offset 来产生等效的末端位移")
    print(f"   e) 7-DOF 臂的冗余自由度允许 offset 组合产生相似末端效果但不同的臂形")
    print(f"   f) 棋盘误差(pre-opt {e_pos_nom_mean*1000:.0f}mm)是 FK 系统误差，offset 只能通过")
    print(f"      改变相机位姿来补偿——但这本质上应该是 l_hand_camera_joint 的职责")
    print(f"   g) 正确方案：解锁 l_hand_camera_joint x/y/z 3DOF，只标定 arm joint offsets 的合理范围")

    return 0


if __name__ == "__main__":
    sys.exit(main())
