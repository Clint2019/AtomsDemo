import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./style.css";
import "./atoms.css";

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
