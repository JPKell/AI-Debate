/**
 * Purpose: Controller for the login and first-run admin bootstrap registration page.
 * Role: page
 * Patterns: structured-errors
 * Depends on: api.js
 * Exposes: none
 * Tests: none
 * Invariants: Never stores plaintext passwords; redirects to `/` immediately after authentication succeeds.
 */

import { apiRequest } from "./api.js";

const el = (id) => document.getElementById(id);

let mode = "login"; // "login" | "register" | "bootstrap"
let openRegistration = false;

function showError(msg) {
  el("auth-banner-text").textContent = msg;
  el("auth-banner").classList.remove("hidden");
}

function hideError() {
  el("auth-banner").classList.add("hidden");
}

function renderMode() {
  hideError();
  const heading = el("auth-heading");
  const subtitle = el("auth-subtitle");
  const submit = el("auth-submit");
  const switchWrap = el("auth-switch-wrap");
  const switchBtn = el("btn-switch-mode");

  if (mode === "bootstrap") {
    heading.textContent = "Create Admin Account";
    subtitle.textContent =
      "Welcome to AI-Debate! Create the initial administrator account to get started.";
    submit.textContent = "Create Admin & Continue";
    switchWrap.classList.add("hidden");
  } else if (mode === "register") {
    heading.textContent = "Create Account";
    subtitle.textContent = "Register a personal account to save debates and custom prompts.";
    submit.textContent = "Register";
    switchWrap.classList.remove("hidden");
    switchBtn.textContent = "Already have an account? Sign In";
  } else {
    heading.textContent = "Sign In";
    subtitle.textContent = "Enter your credentials to access your debate workspace.";
    submit.textContent = "Sign In";
    switchWrap.classList.toggle("hidden", !openRegistration);
    switchBtn.textContent = "Need an account? Register";
  }
}

async function init() {
  try {
    const status = await apiRequest("/api/auth/status");
    if (status.user) {
      window.location.href = "/";
      return;
    }
    openRegistration = Boolean(status.open_registration);
    mode = status.has_users ? "login" : "bootstrap";
    renderMode();
  } catch (err) {
    showError(err.message);
  }

  el("btn-switch-mode").addEventListener("click", () => {
    mode = mode === "login" ? "register" : "login";
    renderMode();
  });

  el("auth-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    hideError();
    const username = el("auth-username").value.trim();
    const password = el("auth-password").value;
    const endpoint = mode === "login" ? "/api/auth/login" : "/api/auth/register";
    try {
      await apiRequest(endpoint, {
        method: "POST",
        body: { username, password },
      });
      window.location.href = "/";
    } catch (err) {
      showError(err.message);
    }
  });
}

init();
