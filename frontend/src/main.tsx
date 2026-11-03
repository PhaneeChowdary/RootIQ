import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./theme.css";
import App from "./App";

if (navigator.platform.startsWith("Win")) document.documentElement.classList.add("os-windows");  // in the bundle: the CSP allows no inline script

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
