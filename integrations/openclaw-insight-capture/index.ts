import { Type } from "typebox";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type StatusAction = "status" | "list_recordings";
type RecordingAction = "start_recording" | "stop_recording";
type CaptureAction = StatusAction | RecordingAction;

type PluginConfig = {
  baseUrl?: string;
  timeoutMs?: number;
};

type JsonObject = Record<string, unknown>;

function normalizedBaseUrl(value: unknown): string {
  const baseUrl = typeof value === "string" ? value.trim() : "";
  if (!baseUrl) return "http://127.0.0.1:8765";
  const parsed = new URL(baseUrl);
  if (parsed.protocol !== "http:") {
    throw new Error("Insight Capture baseUrl must use http on the local host");
  }
  if (!["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)) {
    throw new Error("Insight Capture baseUrl must resolve to the local loopback host");
  }
  return baseUrl.replace(/\/+$/, "");
}

function timeoutMs(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 5000;
  return Math.max(100, Math.min(30000, Math.trunc(value)));
}

async function requestJson(
  baseUrl: string,
  timeout: number,
  path: string,
  method = "GET",
): Promise<JsonObject> {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers: { Accept: "application/json" },
    signal: AbortSignal.timeout(timeout),
  });
  const text = await response.text();
  let payload: JsonObject = {};
  if (text) {
    try {
      payload = JSON.parse(text) as JsonObject;
    } catch {
      throw new Error(`Insight dashboard returned non-JSON data (${response.status})`);
    }
  }
  if (!response.ok) {
    const detail = typeof payload.error === "string" ? payload.error : response.statusText;
    throw new Error(`Insight dashboard rejected the request (${response.status}): ${detail}`);
  }
  return payload;
}

function summarize(action: CaptureAction, payload: JsonObject): string {
  if (action === "start_recording") {
    return `录制已开始：${String(payload.output_path ?? "路径待生成")}`;
  }
  if (action === "stop_recording") {
    if (payload.recording) return "录制仍在进行。";
    return `录制已停止：${String(payload.output_path ?? "正在合并输出")}`;
  }
  if (action === "list_recordings") {
    const bags = Array.isArray(payload.bags) ? payload.bags : [];
    return `当前共有 ${bags.length} 个录制包。`;
  }
  const recording = payload.recording as JsonObject | undefined;
  const cameras = payload.cameras as JsonObject | undefined;
  const captureCheck = payload.captureCheck as JsonObject | undefined;
  const cameraList = Array.isArray(cameras?.cameras) ? cameras.cameras : [];
  const active = Boolean(recording?.recording);
  const stationState = String(captureCheck?.last_result && typeof captureCheck.last_result === "object"
    ? (captureCheck.last_result as JsonObject).state ?? captureCheck.state ?? "未知"
    : captureCheck?.state ?? "未知");
  return `${active ? "正在录制" : "当前空闲"}，检测到 ${cameraList.length} 路相机，检测位状态 ${stationState}。`;
}

export default definePluginEntry({
  id: "insight-capture",
  name: "Insight Capture",
  description: "Safely inspect and control the local Insight data-capture dashboard",
  register(api) {
    const config = (api.pluginConfig ?? {}) as PluginConfig;
    const baseUrl = normalizedBaseUrl(config.baseUrl);
    const timeout = timeoutMs(config.timeoutMs);

    api.registerTool(
      {
        name: "insight_capture_status",
        label: "Insight Capture Status",
        description:
          "Read the local data-capture system status or list recorded bags. " +
          "This tool has no start or stop action.",
        parameters: Type.Object({
          action: Type.Union([
            Type.Literal("status"),
            Type.Literal("list_recordings"),
          ]),
        }),
        async execute(_id, params) {
          const action = params.action as StatusAction;
          let payload: JsonObject;
          if (action === "list_recordings") {
            payload = await requestJson(baseUrl, timeout, "/api/rosbags");
          } else {
            const [recording, cameras, mapping, captureCheck] = await Promise.all([
              requestJson(baseUrl, timeout, "/api/recording/status"),
              requestJson(baseUrl, timeout, "/api/cameras"),
              requestJson(baseUrl, timeout, "/api/mapping"),
              requestJson(baseUrl, timeout, "/api/capture-check"),
            ]);
            payload = { recording, cameras, mapping, captureCheck };
          }
          return {
            content: [{ type: "text", text: summarize(action, payload) }],
            details: { action, payload },
          };
        },
      },
      { optional: true },
    );

    api.registerTool(
      {
        name: "insight_capture_recording",
        label: "Insight Capture Recording",
        description:
          "Start a default-topic recording or stop only an OpenClaw-owned recording. " +
          "Use this tool only when the user explicitly asks for that action.",
        parameters: Type.Object({
          action: Type.Union([
            Type.Literal("start_recording"),
            Type.Literal("stop_recording"),
          ]),
        }),
        async execute(_id, params) {
          const action = params.action as RecordingAction;
          const path =
            action === "start_recording"
              ? "/api/automation/recording/start"
              : "/api/automation/recording/stop";
          const payload = await requestJson(baseUrl, timeout, path, "POST");
          return {
            content: [{ type: "text", text: summarize(action, payload) }],
            details: { action, payload },
          };
        },
      },
      { optional: true },
    );
  },
});
