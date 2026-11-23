/**
 * Purpose: Fetch wrapper for AI-Debate REST APIs with structured error normalization.
 * Role: component
 * Patterns: structured-errors
 * Depends on: none
 * Exposes: ApiError, apiRequest, formatErrorMessage
 * Tests: none
 * Invariants: Never caches or stores credentials in client storage; normalizes structured errors and validation arrays into readable strings (never "[object Object]").
 */

export function formatErrorMessage(value, fallback = "Request failed.") {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed || fallback;
  }
  if (value instanceof Error) {
    return formatErrorMessage(value.message, fallback);
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => {
        if (typeof item === "string") return item.trim();
        if (item && typeof item === "object") {
          if (typeof item.msg === "string") {
            const loc = Array.isArray(item.loc)
              ? item.loc
                  .map((p) => String(p))
                  .filter((p) => p !== "body" && p !== "query" && p !== "path" && p !== "header")
                  .join(".")
              : "";
            return loc ? `${loc}: ${item.msg}` : item.msg;
          }
          if (item.message !== undefined) return formatErrorMessage(item.message, "");
          if (item.detail !== undefined) return formatErrorMessage(item.detail, "");
          try {
            return JSON.stringify(item);
          } catch {
            return "";
          }
        }
        return item != null ? String(item) : "";
      })
      .filter(Boolean);
    return parts.length ? parts.join("; ") : fallback;
  }
  if (value && typeof value === "object") {
    if (value.message !== undefined) return formatErrorMessage(value.message, fallback);
    if (value.detail !== undefined) return formatErrorMessage(value.detail, fallback);
    if (value.msg !== undefined) return formatErrorMessage(value.msg, fallback);
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  if (value == null) {
    return fallback;
  }
  return String(value);
}

export class ApiError extends Error {
  constructor(message, { status = 500, code = "request_failed", details = {} } = {}) {
    super(formatErrorMessage(message, `Request failed (${status})`));
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function apiRequest(path, options = {}) {
  const opts = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
  }

  const resp = await fetch(path, opts);
  if (resp.status === 204) {
    return null;
  }

  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }

  if (!resp.ok) {
    const errObj = data && data.error ? data.error : null;
    const rawMessage =
      (errObj && errObj.message !== undefined ? errObj.message : null) ??
      (data && data.detail !== undefined ? data.detail : null) ??
      (text || null);
    const message = formatErrorMessage(rawMessage, `Request failed (${resp.status})`);
    throw new ApiError(message, {
      status: resp.status,
      code: (errObj && errObj.code) || (resp.status === 422 ? "validation_error" : "http_error"),
      details: (errObj && errObj.details) || {},
    });
  }

  return data;
}
