/**
 * Purpose: Fetch wrapper for AI-Debate REST APIs with structured error normalization.
 * Role: component
 * Patterns: structured-errors
 * Depends on: none
 * Exposes: ApiError, apiRequest
 * Tests: none
 * Invariants: Never caches or stores credentials in client storage; parses structured error.message from backend JSON.
 */

export class ApiError extends Error {
  constructor(message, { status = 500, code = "request_failed", details = {} } = {}) {
    super(message);
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
    const message =
      (errObj && errObj.message) ||
      (data && data.detail) ||
      text ||
      `Request failed (${resp.status})`;
    throw new ApiError(String(message), {
      status: resp.status,
      code: (errObj && errObj.code) || "http_error",
      details: (errObj && errObj.details) || {},
    });
  }

  return data;
}
