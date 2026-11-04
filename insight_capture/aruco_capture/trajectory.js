const B = window.BABYLON;
const colors = { left: "#83abff", right: "#ffb77d" };
const DEFAULT_FOV = 0.8; // Babylon's default vertical field of view
const TICK_COLOR = "#7b93af";
// The camera view sits this far behind the head camera along its optical
// axis, so the origin (the camera itself) and its axes stay in sight.
const CAMERA_PULLBACK = 0.6;
const FIT_MARGIN = 1.15; // breathing room around fitted content
const AXIS_LENGTH = 0.15;
let labelSequence = 0;
function makeLabel(scene, text, position, color, options = {}) {
  const { fontSize = 48, width = 0.16, height = 0.08 } = options;
  const id = "label" + labelSequence++ + ":" + text;
  const texture = new B.DynamicTexture(
    id,
    { width: 256, height: 128 },
    scene,
    false,
  );
  texture.hasAlpha = true;
  texture.drawText(
    text,
    null,
    null,
    `bold ${fontSize}px sans-serif`,
    color,
    "transparent",
    true,
  );
  const material = new B.StandardMaterial(id, scene);
  material.diffuseTexture = texture;
  material.emissiveColor = B.Color3.White();
  material.disableLighting = true;
  material.backFaceCulling = false;
  const plane = B.MeshBuilder.CreatePlane(id, { width, height }, scene);
  plane.material = material;
  plane.billboardMode = B.Mesh.BILLBOARDMODE_ALL;
  plane.position = position;
  plane.isPickable = false;
  return plane;
}
export class Trajectory {
  constructor(canvas, empty) {
    this.empty = empty;
    this.trails = [];
    this.points = [];
    this.current = {};
    this.visible = true;
    try {
      this.engine = new B.Engine(canvas, true, {
        preserveDrawingBuffer: true,
        stencil: true,
      });
      this.engine.setHardwareScalingLevel(
        Math.max(1, window.devicePixelRatio / 1.5),
      );
      this.scene = new B.Scene(this.engine);
      this.scene.useRightHandedSystem = true;
      this.scene.clearColor = new B.Color4(0.078, 0.13, 0.21, 1);
      this.camera = new B.ArcRotateCamera(
        "orbit",
        -0.9,
        1.03,
        1.9,
        new B.Vector3(0, 0, 0.15),
        this.scene,
      );
      this.camera.upVector = new B.Vector3(0, 0, 1);
      this.camera.attachControl(canvas, true);
      this.camera.lowerRadiusLimit = 0.08;
      this.camera.upperRadiusLimit = 100;
      this.camera.minZ = 0.001;
      this.camera.wheelDeltaPercentage = 0.01;
      this.camera.panningSensibility = 1200;
      // Orthographic views size their frustum from the orbit radius, so the
      // wheel and zoom buttons work the same as in perspective views.
      this.scene.onBeforeRenderObservable.add(() => {
        const c = this.camera;
        if (c.mode !== B.Camera.ORTHOGRAPHIC_CAMERA) return;
        const h = c.radius * Math.tan(c.fov / 2);
        const w = h * this.engine.getAspectRatio(c);
        [c.orthoTop, c.orthoBottom, c.orthoLeft, c.orthoRight] = [h, -h, -w, w];
      });
      const grid = [];
      for (let i = -10; i <= 10; i++) {
        const v = i / 10;
        grid.push(
          [new B.Vector3(v, -1, 0), new B.Vector3(v, 1, 0)],
          [new B.Vector3(-1, v, 0), new B.Vector3(1, v, 0)],
        );
      }
      // The grid and its meter ticks move together to setFloor()'s height.
      this.floor = new B.TransformNode("floor", this.scene);
      this.floorZ = 0;
      const floor = B.MeshBuilder.CreateLineSystem(
        "grid",
        { lines: grid },
        this.scene,
      );
      floor.color = B.Color3.FromHexString("#2b4058");
      floor.parent = this.floor;
      // Dashed drop from the origin to the lowered grid, scaled to its depth.
      this.drop = B.MeshBuilder.CreateDashedLines(
        "originDrop",
        {
          points: [B.Vector3.Zero(), new B.Vector3(0, 0, -1)],
          dashNb: 24,
        },
        this.scene,
      );
      this.drop.color = B.Color3.FromHexString("#91b8f7");
      this.drop.isVisible = false;
      const axes = [
        ["X", [AXIS_LENGTH, 0, 0], "#ef9194"],
        ["Y", [0, AXIS_LENGTH, 0], "#81cba1"],
        ["Z", [0, 0, AXIS_LENGTH], "#91b8f7"],
      ];
      for (const [name, pos, color] of axes) {
        const line = B.MeshBuilder.CreateLines(
          "axis" + name,
          { points: [B.Vector3.Zero(), new B.Vector3(...pos)] },
          this.scene,
        );
        line.color = B.Color3.FromHexString(color);
        makeLabel(this.scene, name, new B.Vector3(...pos).scale(1.2), color, {
          fontSize: 64,
          width: 0.035,
          height: 0.035,
        });
      }
      // Scale reference: a couple of meter ticks along +X/+Y on top of the
      // 10 cm floor grid, so distance reads off the canvas itself.
      for (const d of [0.5, 1]) {
        const text = d.toFixed(1) + " m";
        for (const position of [
          new B.Vector3(d, 0.035, 0.001),
          new B.Vector3(0.035, d, 0.001),
        ]) {
          makeLabel(this.scene, text, position, TICK_COLOR, {
            fontSize: 40,
            width: 0.1,
            height: 0.05,
          }).parent = this.floor;
        }
      }
      this.tips = {};
      for (const side of ["left", "right"]) {
        const tip = B.MeshBuilder.CreateSphere(
          side,
          { diameter: 0.018, segments: 12 },
          this.scene,
        );
        const material = new B.StandardMaterial(side, this.scene);
        material.emissiveColor = B.Color3.FromHexString(colors[side]);
        material.disableLighting = true;
        tip.material = material;
        tip.isVisible = false;
        const axesMesh = B.MeshBuilder.CreateLineSystem(
          side + "Orientation",
          {
            lines: Array.from({ length: 3 }, () => [
              B.Vector3.Zero(),
              B.Vector3.Zero(),
            ]),
            colors: axes.map(([, , c]) => [
              B.Color4.FromColor3(B.Color3.FromHexString(c)),
              B.Color4.FromColor3(B.Color3.FromHexString(c)),
            ]),
            updatable: true,
          },
          this.scene,
        );
        axesMesh.isVisible = false;
        this.tips[side] = { tip, axes: axesMesh };
      }
      this.view("camera");
      this.resize = new ResizeObserver(() => this.engine.resize());
      this.resize.observe(canvas);
      this.lastRender = 0;
      this.engine.runRenderLoop(() => {
        const now = performance.now();
        if (this.visible && !document.hidden && now - this.lastRender > 45) {
          this.scene.render();
          this.lastRender = now;
        }
      });
    } catch (e) {
      this.empty.replaceChildren();
      const strong = document.createElement("strong");
      strong.textContent = "无法初始化 3D 视图";
      const hint = document.createElement("span");
      hint.textContent = "请启用浏览器硬件加速后重试，视频和标注仍可使用。";
      this.empty.append(strong, hint);
      this.failed = true;
    }
  }
  setVisible(value) {
    this.visible = value;
    if (value && this.engine) this.engine.resize();
  }
  setData(rows, fit = false) {
    if (this.failed) return;
    this.trails.forEach((m) => m.dispose());
    this.trails = [];
    this.points = [];
    for (const side of ["left", "right"]) {
      const lines = [];
      let part = [],
        previous = null;
      const flush = () => {
        if (part.length > 1) lines.push(part);
        part = [];
      };
      // Keep invalid samples as breaks when reducing a long recording for drawing.
      const stride = Math.max(1, Math.ceil(rows.length / 4000));
      rows.forEach((row, i) => {
        const pose = row.arms[side];
        if (!pose?.valid) {
          flush();
          previous = null;
          return;
        }
        if (previous !== null && row.t - previous > 0.3) flush();
        previous = row.t;
        if (i % stride && i !== rows.length - 1) return;
        const p = new B.Vector3(...pose.position);
        part.push(p);
        this.points.push(p);
      });
      flush();
      if (lines.length) {
        const line = B.MeshBuilder.CreateLineSystem(
          side + "Trail",
          { lines },
          this.scene,
        );
        line.color = B.Color3.FromHexString(colors[side]);
        this.trails.push(line);
      }
    }
    this.empty.hidden = this.points.length > 0;
    // Gravity-aligned trajectories run below the head camera at the origin;
    // lay the grid under them (0.1 m steps) so the camera view sees it. Live
    // updates only lower it, so a moving trail doesn't make it jump.
    let low = 0;
    if (this.cameraView?.gravity_aligned !== false)
      for (const p of this.points) low = Math.min(low, p.z);
    low = Math.floor(low * 10 + 1e-6) / 10;
    this.setFloor(fit ? low : Math.min(this.floorZ, low));
    if (fit) this.view("camera");
  }
  setFloor(z) {
    if (z === this.floorZ) return;
    this.floorZ = z;
    this.floor.position.z = z;
    this.drop.isVisible = z < 0;
    this.drop.scaling.z = -z;
    this.floorLabel?.dispose();
    this.floorLabel =
      z < 0
        ? makeLabel(
            this.scene,
            `z ${z.toFixed(1)} m`,
            new B.Vector3(0.07, -0.03, z + 0.001),
            TICK_COLOR,
            { fontSize: 40, width: 0.1, height: 0.05 },
          )
        : null;
  }
  cursor(row) {
    if (this.failed) return;
    for (const side of ["left", "right"]) {
      const p = row?.arms?.[side],
        m = this.tips[side];
      m.tip.isVisible = !!p?.valid;
      m.axes.isVisible = !!p?.valid;
      if (!p?.valid) continue;
      const point = new B.Vector3(...p.position);
      m.tip.position.copyFrom(point);
      const lines = [0, 1, 2].map((axis) => [
        point,
        point.add(
          new B.Vector3(...p.rotation.map((r) => r[axis])).scale(0.065),
        ),
      ]);
      B.MeshBuilder.CreateLineSystem(side + "Orientation", {
        lines,
        instance: m.axes,
      });
    }
  }
  setCameraView(view) {
    const key = JSON.stringify(view ?? null);
    if (this.failed || key === this.cameraKey) return;
    this.cameraKey = key;
    this.cameraView = view;
    if (this.currentView === "camera") this.view("camera");
  }
  view(kind) {
    if (this.failed) return;
    this.currentView = kind;
    if (kind === "camera") {
      // Look along the head camera's optical axis from just behind it.
      // reference_from_rgb holds its optical axes and origin in the
      // trajectory frame; gravity-aligned sessions keep z up, older ones
      // (optical reference frame) use image up.
      const m = this.cameraView?.reference_from_rgb;
      const column = (j) => new B.Vector3(m[0][j], m[1][j], m[2][j]);
      const position = m ? column(3) : B.Vector3.Zero();
      const forward = m ? column(2) : new B.Vector3(0, 0.8, -0.6);
      this.camera.upVector =
        m && this.cameraView.gravity_aligned === false
          ? column(1).scale(-1)
          : new B.Vector3(0, 0, 1);
      this.camera.mode = B.Camera.PERSPECTIVE_CAMERA;
      this.camera.fov = this.cameraView?.fov_y_rad ?? DEFAULT_FOV;
      this.camera.setTarget(position);
      this.camera.setPosition(position.subtract(forward.scale(CAMERA_PULLBACK)));
      return;
    }
    this.camera.fov = DEFAULT_FOV;
    this.camera.upVector = new B.Vector3(0, 0, 1);
    // Fit the trajectories together with the origin (the head camera).
    let min = B.Vector3.Zero(),
      max = B.Vector3.Zero();
    for (const p of this.points) {
      min = B.Vector3.Minimize(min, p);
      max = B.Vector3.Maximize(max, p);
    }
    if (!this.points.length) {
      min = new B.Vector3(-0.3, -0.3, -0.3);
      max = new B.Vector3(0.3, 0.3, 0.3);
    }
    const size = max.subtract(min);
    const tanV = Math.tan(this.camera.fov / 2);
    const tanH = tanV * this.engine.getAspectRatio(this.camera);
    // Eye direction from the target, and which box axes end up on screen
    // as [horizontal, vertical, depth]. With z up and y forward: top looks
    // down with y up the screen, front looks forward from behind, side
    // looks from the right with y pointing right.
    const views = {
      top: [new B.Vector3(0, -0.015, 1), ["x", "y", "z"]],
      front: [new B.Vector3(0, -1, 0), ["x", "z", "y"]],
      side: [new B.Vector3(1, 0, 0), ["y", "z", "x"]],
    };
    let direction, radius;
    // The three views are orthographic, so near content isn't enlarged.
    this.camera.mode = views[kind]
      ? B.Camera.ORTHOGRAPHIC_CAMERA
      : B.Camera.PERSPECTIVE_CAMERA;
    if (views[kind]) {
      const [h, v, d] = views[kind][1];
      direction = views[kind][0];
      // The eye must also stay in front of the nearest content.
      radius = Math.max(
        Math.max(size[h] / 2 / tanH, size[v] / 2 / tanV) * FIT_MARGIN,
        size[d] / 2 + 0.1,
      );
    } else {
      direction = new B.Vector3(0.45, -0.75, 0.5);
      radius =
        ((size.length() / 2) * FIT_MARGIN) /
        Math.sin(Math.atan(Math.min(tanH, tanV)));
    }
    const target = min.add(max).scale(0.5);
    this.camera.setTarget(target);
    this.camera.setPosition(
      target.add(direction.normalize().scale(Math.max(0.3, radius))),
    );
  }
  zoom(factor) {
    if (!this.failed)
      this.camera.radius = Math.max(
        0.08,
        Math.min(100, this.camera.radius * factor),
      );
  }
}
export function poseFromState(values, offset, valid) {
  const a = values.slice(offset + 3, offset + 6),
    b = values.slice(offset + 6, offset + 9);
  const c = [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
  return {
    valid,
    position: values.slice(offset, offset + 3),
    rotation: [a, b, c],
  };
}
