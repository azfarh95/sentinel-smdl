import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";

// Initialize Telegram WebApp (if running inside the TWA / Mini App).
// expand() forces full-height; ready() tells TG we're done loading.
const tg = (window as any).Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.("#0c0c0e");
  tg.setBackgroundColor?.("#0c0c0e");
}

const app = mount(App, { target: document.getElementById("app")! });
export default app;
