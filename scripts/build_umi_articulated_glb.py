#!/usr/bin/env python3

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh
import trimesh.transformations as tf


ROOT = Path(__file__).resolve().parents[1]
MJCF_PATH = ROOT.parent / "mujoco_menagerie" / "umi_gripper" / "umi_gripper.xml"
ASSET_DIR = MJCF_PATH.parent / "assets"
OUTPUT_PATH = ROOT / "assets" / "models" / "UMI_Gripper_articulated.glb"


RGBA_BY_MATERIAL = {
    "white": np.array([235, 235, 235, 255], dtype=np.uint8),
    "orange": np.array([235, 173, 61, 255], dtype=np.uint8),
    "gray": np.array([95, 95, 95, 255], dtype=np.uint8),
    "light_gray": np.array([155, 155, 155, 255], dtype=np.uint8),
    "black": np.array([20, 20, 20, 255], dtype=np.uint8),
}


def mujoco_transform(element: ET.Element) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    if "quat" in element.attrib:
        w, x, y, z = (float(value) for value in element.attrib["quat"].split())
        quat = np.array([x, y, z, w], dtype=np.float64)
        quat /= np.linalg.norm(quat)
        transform = tf.quaternion_matrix(quat)
    if "pos" in element.attrib:
        pos = np.array([float(value) for value in element.attrib["pos"].split()], dtype=np.float64)
        transform[:3, 3] = pos
    return transform


def load_mesh(mesh_file: Path, material_name: str) -> trimesh.Trimesh:
    mesh = trimesh.load(mesh_file, force="mesh")
    mesh.visual.face_colors = RGBA_BY_MATERIAL.get(material_name, RGBA_BY_MATERIAL["white"])
    return mesh


def main() -> None:
    tree = ET.parse(MJCF_PATH)
    root = tree.getroot()

    asset_meshes = {}
    for mesh in root.findall("./asset/mesh"):
        file_name = mesh.attrib.get("file")
        if file_name:
            mesh_path = ASSET_DIR / file_name
            name = mesh.attrib.get("name") or Path(file_name).stem
            asset_meshes[name] = mesh_path

    top_body = root.find("./worldbody/body")
    if top_body is None:
        raise RuntimeError("Failed to find top-level gripper body in MJCF")

    scene = trimesh.Scene()
    scene.graph.update(frame_to="umi_root")

    root_transform = mujoco_transform(top_body)

    def add_geom(
        mesh_name: str,
        node_name: str,
        parent_node_name: str,
        transform: np.ndarray,
        material_name: str,
    ) -> None:
        mesh_file = asset_meshes[mesh_name]
        mesh = load_mesh(mesh_file, material_name)
        scene.add_geometry(
            mesh,
            node_name=node_name,
            geom_name=node_name,
            parent_node_name=parent_node_name,
            transform=transform,
        )

    add_geom("base_link", "base_link", "umi_root", root_transform, "white")

    for geom in top_body.findall("./geom"):
        mesh_name = geom.attrib.get("mesh")
        if mesh_name == "gopro":
            add_geom(
                mesh_name,
                "gopro",
                "umi_root",
                root_transform @ mujoco_transform(geom),
                geom.attrib.get("material", "gray"),
            )

    for body_name in ("left_finger_holder", "right_finger_holder"):
        body = top_body.find(f"./body[@name='{body_name}']")
        if body is None:
            raise RuntimeError(f"Failed to find body {body_name}")

        slider_name = body_name.replace("_holder", "_slider")
        slider_transform = root_transform @ mujoco_transform(body)
        scene.graph.update(frame_to=slider_name, frame_from="umi_root", matrix=slider_transform)

        for geom in body.findall("./geom"):
            mesh_name = geom.attrib.get("mesh")
            if not mesh_name:
                continue
            local_transform = mujoco_transform(geom)
            material_name = geom.attrib.get("material", "white")
            if mesh_name.endswith("_holder"):
                node_name = f"{body_name}_{mesh_name}"
                add_geom(
                    mesh_name,
                    node_name,
                    "umi_root",
                    slider_transform @ local_transform,
                    material_name,
                )
            else:
                node_name = f"{slider_name}_{mesh_name}"
                add_geom(mesh_name, node_name, slider_name, local_transform, material_name)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(scene.export(file_type="glb"))
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
