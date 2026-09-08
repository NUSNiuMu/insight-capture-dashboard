const B = window.BABYLON;
const colors = { left: "#83abff", right: "#ffb77d" };
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
      const grid = [];
      for (let i = -10; i <= 10; i++) {
        const v = i / 10;
        grid.push(
          [new B.Vector3(v, -1, 0), new B.Vector3(v, 1, 0)],
          [new B.Vector3(-1, v, 0), new B.Vector3(1, v, 0)],
        );
      }
      const floor = B.MeshBuilder.CreateLineSystem(
        "grid",
        { lines: grid },
        this.scene,
      );
      floor.color = B.Color3.FromHexString("#2b4058");
      const axes = [
        ["X", [0.3, 0, 0], "#ef9194"],
        ["Y", [0, 0.3, 0], "#81cba1"],
        ["Z", [0, 0, 0.3], "#91b8f7"],
      ];
      for (const [name, pos, color] of axes) {
        const line = B.MeshBuilder.CreateLines(
          "axis" + name,
          { points: [B.Vector3.Zero(), new B.Vector3(...pos)] },
          this.scene,
        );
        line.color = B.Color3.FromHexString(color);
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
    if (fit) this.view("free");
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
  view(kind) {
    if (this.failed) return;
    if (this.points.length) {
      let min = this.points[0].clone(),
        max = min.clone();
      for (const p of this.points) {
        min = B.Vector3.Minimize(min, p);
        max = B.Vector3.Maximize(max, p);
      }
      this.camera.setTarget(min.add(max).scale(0.5));
      this.camera.radius = Math.max(0.4, B.Vector3.Distance(min, max) * 1.6);
    } else {
      this.camera.setTarget(new B.Vector3(0, 0, 0.12));
      this.camera.radius = 1.9;
    }
    const angles = {
      free: [-0.9, 1.03],
      top: [-Math.PI / 2, 0.015],
      front: [-Math.PI / 2, Math.PI / 2],
      side: [0, Math.PI / 2],
    };
    [this.camera.alpha, this.camera.beta] = angles[kind] || angles.free;
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
