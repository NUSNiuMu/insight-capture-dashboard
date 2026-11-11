import { createApp, h, reactive } from "vue";
import CameraPreview from "./components/CameraPreview.vue";
import "./preview.css";
export function mountPreview(element) {
  const state = reactive({ preview: {}, online: false, recording: false, active: false });
  const app = createApp({ render: () => h(CameraPreview, state) });
  app.mount(element);
  return { update: values => Object.assign(state, values), stop: () => { state.active = false; } };
}
