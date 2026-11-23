/**
 * Purpose: Admin panel page controller managing encrypted openWeightGym settings, Test Connection, force-stop, and users.
 * Role: page
 * Patterns: structured-errors
 * Depends on: api.js, markdown.js
 * Exposes: none
 * Tests: none
 * Invariants: Never receives or displays plaintext API keys from the backend.
 */

import { apiRequest, formatErrorMessage } from "./api.js";
import { escapeHtml } from "./markdown.js";

const el = (id) => document.getElementById(id);

function showBanner(text, isError = false) {
  const b = el("admin-banner");
  el("admin-banner-text").textContent = formatErrorMessage(text, "");
  b.classList.remove("hidden", "banner-error");
  if (isError) b.classList.add("banner-error");
}

function hideBanner() {
  el("admin-banner").classList.add("hidden");
}

async function loadSettings() {
  const s = await apiRequest("/api/admin/settings");
  el("cfg-protocol").value = s.api_protocol || "http";
  el("cfg-host").value = s.api_host || "127.0.0.1";
  el("cfg-port").value = s.api_port || 8442;
  el("cfg-api-key").value = "";
  el("cfg-key-hint").textContent = s.api_key_configured
    ? "An encrypted API key is currently stored."
    : "No API key stored yet.";
  el("cfg-suite-app").value = s.suite_application || "ai-debate";
  el("cfg-node").value = s.default_node || "";
  el("cfg-priority").value = s.default_priority || "";
  el("cfg-sensitivity").value = s.default_sensitivity || "";
  el("cfg-log-off").checked = Boolean(s.log_off);
  el("cfg-no-wake").checked = Boolean(s.no_wake);
  el("cfg-open-reg").checked = Boolean(s.open_registration);
}

async function loadBusyStatus() {
  const st = await apiRequest("/api/admin/status");
  const label = el("server-busy-label");
  const btn = el("btn-force-stop");
  if (st.busy) {
    label.textContent = "Server is currently running an active debate.";
    btn.disabled = false;
  } else {
    label.textContent = "Server is idle (no active debate running).";
    btn.disabled = true;
  }
}

async function loadUsers() {
  const users = await apiRequest("/api/admin/users");
  const wrap = el("users-table-wrap");
  wrap.innerHTML = `
    <table class="users-table">
      <thead>
        <tr>
          <th>Username</th>
          <th>Role</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${users
          .map(
            (u) => `
          <tr>
            <td><strong>${escapeHtml(u.username)}</strong></td>
            <td>${escapeHtml(u.role)}</td>
            <td>${u.is_active ? "Active" : "Disabled"}</td>
            <td class="table-actions">
              <button class="btn btn-ghost btn-sm" data-toggle-role="${escapeHtml(u.id)}" data-role="${u.role === "admin" ? "user" : "admin"}">
                Make ${u.role === "admin" ? "User" : "Admin"}
              </button>
              <button class="btn btn-ghost btn-sm" data-toggle-active="${escapeHtml(u.id)}" data-active="${u.is_active ? "0" : "1"}">
                ${u.is_active ? "Disable" : "Enable"}
              </button>
              <button class="btn btn-ghost btn-sm" data-reset-password="${escapeHtml(u.id)}" data-username="${escapeHtml(u.username)}">
                Reset Password
              </button>
            </td>
          </tr>
        `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function bindEvents() {
  el("btn-dismiss-admin-banner").addEventListener("click", hideBanner);
  el("btn-admin-logout").addEventListener("click", async () => {
    await apiRequest("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  });

  el("settings-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    hideBanner();
    const body = {
      api_protocol: el("cfg-protocol").value,
      api_host: el("cfg-host").value.trim(),
      api_port: Number(el("cfg-port").value),
      suite_application: el("cfg-suite-app").value.trim() || "ai-debate",
      default_node: el("cfg-node").value.trim(),
      default_priority: el("cfg-priority").value,
      default_sensitivity: el("cfg-sensitivity").value,
      log_off: el("cfg-log-off").checked,
      no_wake: el("cfg-no-wake").checked,
      open_registration: el("cfg-open-reg").checked,
    };
    const keyVal = el("cfg-api-key").value.trim();
    if (keyVal) body.api_key = keyVal;

    try {
      await apiRequest("/api/admin/settings", { method: "PATCH", body });
      await loadSettings();
      showBanner("Settings saved and applied to live openWeightGym client.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  el("btn-test-conn").addEventListener("click", async () => {
    hideBanner();
    const body = {
      api_protocol: el("cfg-protocol").value,
      api_host: el("cfg-host").value.trim(),
      api_port: Number(el("cfg-port").value),
      suite_application: el("cfg-suite-app").value.trim() || "ai-debate",
    };
    const keyVal = el("cfg-api-key").value.trim();
    if (keyVal) body.api_key = keyVal;

    try {
      const res = await apiRequest("/api/admin/test-connection", { method: "POST", body });
      showBanner(
        `Connected to ${res.endpoint} — ${res.chat_model_count} chat models available (${res.models.join(", ") || "none"}).`,
        false
      );
    } catch (err) {
      showBanner(`Connection test failed: ${err.message}`, true);
    }
  });

  el("btn-refresh-status").addEventListener("click", loadBusyStatus);
  el("btn-force-stop").addEventListener("click", async () => {
    try {
      const res = await apiRequest("/api/admin/force-stop", { method: "POST" });
      await loadBusyStatus();
      showBanner(
        res.stopped ? "Active debate force-stopped." : "No active debate was running.",
        false
      );
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  el("create-user-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    hideBanner();
    try {
      await apiRequest("/api/admin/users", {
        method: "POST",
        body: {
          username: el("new-username").value.trim(),
          password: el("new-password").value,
          role: el("new-role").value,
        },
      });
      el("new-username").value = "";
      el("new-password").value = "";
      await loadUsers();
      showBanner("User created.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  el("users-table-wrap").addEventListener("click", async (ev) => {
    const roleBtn = ev.target.closest("[data-toggle-role]");
    if (roleBtn) {
      const uid = roleBtn.getAttribute("data-toggle-role");
      const role = roleBtn.getAttribute("data-role");
      try {
        await apiRequest(`/api/admin/users/${encodeURIComponent(uid)}`, {
          method: "PATCH",
          body: { role },
        });
        await loadUsers();
      } catch (err) {
        showBanner(err.message, true);
      }
      return;
    }
    const activeBtn = ev.target.closest("[data-toggle-active]");
    if (activeBtn) {
      const uid = activeBtn.getAttribute("data-toggle-active");
      const is_active = activeBtn.getAttribute("data-active") === "1";
      try {
        await apiRequest(`/api/admin/users/${encodeURIComponent(uid)}`, {
          method: "PATCH",
          body: { is_active },
        });
        await loadUsers();
      } catch (err) {
        showBanner(err.message, true);
      }
      return;
    }
    const resetBtn = ev.target.closest("[data-reset-password]");
    if (resetBtn) {
      const uid = resetBtn.getAttribute("data-reset-password");
      const uname = resetBtn.getAttribute("data-username") || "user";
      const newPassword = window.prompt(`Enter a new password for ${uname} (at least 6 characters):`);
      if (newPassword === null) return;
      if (newPassword.length < 6) {
        showBanner("Password must be at least 6 characters long.", true);
        return;
      }
      try {
        await apiRequest(`/api/admin/users/${encodeURIComponent(uid)}`, {
          method: "PATCH",
          body: { password: newPassword },
        });
        showBanner(`Password updated for ${uname}.`, false);
      } catch (err) {
        showBanner(err.message, true);
      }
    }
  });
}

async function init() {
  bindEvents();
  try {
    await Promise.all([loadSettings(), loadBusyStatus(), loadUsers()]);
  } catch (err) {
    showBanner(err.message, true);
  }
}

init();
