import { createElement } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, parseSse } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("public SSE protocol", () => {
  it("decodes CRLF and multiple data lines", () => {
    const event = parseSse(
      'event: stage.heartbeat\r\ndata: {"sequence":2,"data":\r\ndata: {"stage":"retrieval"}}\r\n',
    );
    expect(event?.event).toBe("stage.heartbeat");
    expect(event?.data.sequence).toBe(2);
    expect(event?.data.data?.stage).toBe("retrieval");
  });

  it("ignores comments and rejects malformed JSON", () => {
    expect(parseSse(": heartbeat\n\nevent: run.completed\ndata: {}\n")).toEqual(
      {
        event: "run.completed",
        data: {},
      },
    );
    expect(() => parseSse("event: run.failed\ndata: {broken\n")).toThrow(
      SyntaxError,
    );
  });

  it("renders a localized degraded readiness status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string | URL | Request) => {
        const url = String(input);
        if (url.endsWith("/ready")) {
          return Promise.resolve(
            new Response(JSON.stringify({ status: "degraded" }), {
              status: 200,
            }),
          );
        }
        if (url.endsWith("/api/v1/auth/session")) {
          return Promise.resolve(
            new Response(JSON.stringify({ authenticated: false }), {
              status: 200,
            }),
          );
        }
        return Promise.reject(new Error(`unexpected request: ${url}`));
      }),
    );

    render(createElement(App));

    await waitFor(() => {
      expect(screen.getByLabelText("Status: degraded")).toHaveClass(
        "status",
        "degraded",
      );
    });
    expect(screen.getByText("degraded")).toBeVisible();
  });
});
