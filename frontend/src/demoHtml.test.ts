import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it, vi } from "vitest";

const demoHtmlPath = resolve(process.cwd(), "public/demo.html");
const demoHtml = readFileSync(demoHtmlPath, "utf8");
const bodyMatch = demoHtml.match(/<body>([\s\S]*)<\/body>/i);
const inlineScripts = [...demoHtml.matchAll(/<script(?:\s+[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map((match) => match[1].trim())
  .filter(Boolean);

if (!bodyMatch || inlineScripts.length === 0) {
  throw new Error("Failed to parse demo.html test fixture");
}

const bodyMarkup = bodyMatch[1].replace(/<script[\s\S]*?<\/script>/gi, "");
const inlineScript = inlineScripts.at(-1)!;

type DemoHarness = {
  api: Record<string, (...args: any[]) => any>;
  document: Document;
  window: Window & typeof globalThis & Record<string, any>;
  fetchMock: ReturnType<typeof vi.fn>;
  createdRooms: any[];
  playMock: ReturnType<typeof vi.fn>;
  pauseMock: ReturnType<typeof vi.fn>;
  nowMock: ReturnType<typeof vi.fn>;
  clickMock: ReturnType<typeof vi.fn>;
  createObjectUrlMock: ReturnType<typeof vi.fn>;
  revokeObjectUrlMock: ReturnType<typeof vi.fn>;
  close: () => void;
};

function buildHarness(): DemoHarness {
  const dom = new JSDOM(`<!doctype html><html><body>${bodyMarkup}</body></html>`, {
    pretendToBeVisual: true,
    runScripts: "outside-only",
    url: "https://example.test/demo.html",
  });

  const { window } = dom;
  const createdRooms: any[] = [];
  const fetchMock = vi.fn(async () => ({
    ok: true,
    json: async () => ({}),
  }));
  const playMock = vi.fn().mockResolvedValue(undefined);
  const pauseMock = vi.fn();
  const nowMock = vi.fn(() => 1000);
  const clickMock = vi.fn();
  const createObjectUrlMock = vi.fn(() => "blob:demo");
  const revokeObjectUrlMock = vi.fn();

  class MockRoom {
    handlers: Record<string, Array<(...args: any[]) => void>> = {};
    remoteParticipants = new Map();
    canPlaybackAudio = true;
    startAudio = vi.fn().mockResolvedValue(undefined);
    connect = vi.fn().mockResolvedValue(undefined);
    disconnect = vi.fn();
    localParticipant = {
      publishData: vi.fn().mockResolvedValue(undefined),
      setMicrophoneEnabled: vi.fn().mockResolvedValue(undefined),
    };

    constructor() {
      createdRooms.push(this);
    }

    on(event: string, handler: (...args: any[]) => void) {
      this.handlers[event] ??= [];
      this.handlers[event].push(handler);
    }

    emit(event: string, ...args: any[]) {
      for (const handler of this.handlers[event] ?? []) {
        handler(...args);
      }
    }
  }

  Object.assign(window, {
    console,
    fetch: fetchMock,
    TextDecoder,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  });

  Object.defineProperty(window, "isSecureContext", {
    configurable: true,
    value: true,
  });
  Object.defineProperty(window.navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: vi.fn(),
    },
  });
  Object.defineProperty(window.performance, "now", {
    configurable: true,
    value: nowMock,
  });
  Object.defineProperty(window.HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: playMock,
  });
  Object.defineProperty(window.HTMLMediaElement.prototype, "pause", {
    configurable: true,
    value: pauseMock,
  });
  Object.defineProperty(window.HTMLAnchorElement.prototype, "click", {
    configurable: true,
    value: clickMock,
  });
  Object.defineProperty(window.URL, "createObjectURL", {
    configurable: true,
    value: createObjectUrlMock,
  });
  Object.defineProperty(window.URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectUrlMock,
  });
  window.Math.random = vi.fn(() => 0.123456789);
  window.LivekitClient = {
    Room: MockRoom,
    RoomEvent: {
      ConnectionStateChanged: "ConnectionStateChanged",
      ParticipantConnected: "ParticipantConnected",
      TrackPublished: "TrackPublished",
      TrackSubscriptionFailed: "TrackSubscriptionFailed",
      AudioPlaybackStatusChanged: "AudioPlaybackStatusChanged",
      TrackSubscribed: "TrackSubscribed",
      TrackUnsubscribed: "TrackUnsubscribed",
      DataReceived: "DataReceived",
    },
    Track: {
      Kind: {
        Audio: "audio",
      },
    },
    ConnectionState: {
      Connected: "connected",
      Disconnected: "disconnected",
    },
  };

  window.eval(inlineScript);

  return {
    api: window as any,
    createdRooms,
    document: window.document,
    fetchMock,
    window: window as any,
    playMock,
    pauseMock,
    nowMock,
    clickMock,
    createObjectUrlMock,
    revokeObjectUrlMock,
    close: () => window.close(),
  };
}

function inputElement(document: Document, id: string) {
  return document.getElementById(id) as HTMLInputElement;
}

function buttonElement(document: Document, id: string) {
  return document.getElementById(id) as HTMLButtonElement;
}

function textOf(document: Document, id: string) {
  return document.getElementById(id)?.textContent ?? "";
}

function classOf(document: Document, id: string) {
  return document.getElementById(id)?.className ?? "";
}

async function flushAsyncWork(turns = 8) {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("demo.html helpers", () => {
  it.each([
    ["plain text", "plain text"],
    ["a & b", "a &amp; b"],
    ["<tag>", "&lt;tag&gt;"],
    ['say "hi"', "say &quot;hi&quot;"],
    ["<a&b>", "&lt;a&amp;b&gt;"],
    ["&&", "&amp;&amp;"],
    [">>>", "&gt;&gt;&gt;"],
    ["\"quoted\"", "&quot;quoted&quot;"],
    ["mix <>&\"", "mix &lt;&gt;&amp;&quot;"],
    ["already safe", "already safe"],
    ["x < y & y > z", "x &lt; y &amp; y &gt; z"],
    ["multi & <tag> \"q\"", "multi &amp; &lt;tag&gt; &quot;q&quot;"],
  ])("escHtml(%j)", (value, expected) => {
    const harness = buildHarness();
    expect(harness.api.escHtml(value)).toBe(expected);
    harness.close();
  });

  it.each([
    ["hello", "hello"],
    ["  hello", "hello"],
    ["hello  ", "hello"],
    ["hello   world", "hello world"],
    ["\nhello\nworld\n", "hello world"],
    ["tabs\tand\tspaces", "tabs and spaces"],
    ["  many \n kinds \t of  whitespace ", "many kinds of whitespace"],
    ["", ""],
    [null, ""],
    [undefined, ""],
    [" punctuation, stays!  ", "punctuation, stays!"],
    ["a\n\n\nb", "a b"],
  ])("normalizeTranscriptText(%j)", (value, expected) => {
    const harness = buildHarness();
    expect(harness.api.normalizeTranscriptText(value)).toBe(expected);
    harness.close();
  });

  it.each([
    [0, "0:00"],
    [1, "0:01"],
    [9, "0:09"],
    [10, "0:10"],
    [59, "0:59"],
    [60, "1:00"],
    [61, "1:01"],
    [65, "1:05"],
    [600, "10:00"],
    [3661, "61:01"],
    [-1, "0:00"],
    [-100, "0:00"],
  ])("fmtCountdown(%p)", (value, expected) => {
    const harness = buildHarness();
    expect(harness.api.fmtCountdown(value)).toBe(expected);
    harness.close();
  });

  it.each([
    [0, "0m 0s"],
    [1, "0m 1s"],
    [5, "0m 5s"],
    [59, "0m 59s"],
    [60, "1m 0s"],
    [61, "1m 1s"],
    [65, "1m 5s"],
    [121.8, "2m 2s"],
  ])("_fmtDuration(%p)", (value, expected) => {
    const harness = buildHarness();
    expect(harness.api._fmtDuration(value)).toBe(expected);
    harness.close();
  });

  it.each([
    ["safe", "safe"],
    ["<b>", "&lt;b&gt;"],
    ["&", "&amp;"],
    ['"', "&quot;"],
    [42, "42"],
    [null, ""],
  ])("_esc(%j)", (value, expected) => {
    const harness = buildHarness();
    expect(harness.api._esc(value)).toBe(expected);
    harness.close();
  });
});

describe("demo.html modal and state", () => {
  it.each([
    ["found", "Resuming", "modal-badge found"],
    ["new", "Starting fresh", "modal-badge new"],
    ["checking", "Checking…", "modal-badge checking"],
    ["other", "Other state", "modal-badge other"],
  ])("setModalBadge(%s)", (type, text, expectedClass) => {
    const harness = buildHarness();
    harness.api.setModalBadge(type, text);
    expect(classOf(harness.document, "modal-badge")).toBe(expectedClass);
    expect(harness.document.getElementById("modal-badge")?.innerHTML).toContain(text);
    harness.close();
  });

  it.each([
    ["idle", "press connect to start", false],
    ["connecting", "connecting…", false],
    ["ready", "ready — speak to begin", false],
    ["listening", "listening…", true],
    ["speaking", "agent speaking…", true],
    ["error", "connection error", false],
    ["unknown", "unknown", false],
    ["idle", "press connect to start", false],
  ])("setAppState(%s)", (state, label, waveformActive) => {
    const harness = buildHarness();
    harness.api.setAppState(state);
    expect(classOf(harness.document, "mic-wrap")).toContain(`state-${state}`);
    expect(classOf(harness.document, "mic-btn")).toContain(state);
    expect(classOf(harness.document, "pill")).toContain(state);
    expect(textOf(harness.document, "pill-text")).toBe(state);
    expect(textOf(harness.document, "state-label")).toBe(label);
    expect(harness.document.getElementById("waveform")?.classList.contains("active")).toBe(waveformActive);
    harness.close();
  });

  it.each([
    [180, 120, 30, false, false],
    [120, 120, 30, true, false],
    [119, 120, 30, true, false],
    [31, 120, 30, true, false],
    [30, 120, 30, false, true],
    [5, 120, 30, false, true],
  ])("updateCountdownClasses(%p)", (secs, warnAt, critAt, warnExpected, critExpected) => {
    const harness = buildHarness();
    const badge = harness.document.getElementById("cd-session")!;
    harness.api.updateCountdownClasses(badge, secs, warnAt, critAt);
    expect(badge.classList.contains("warn")).toBe(warnExpected);
    expect(badge.classList.contains("crit")).toBe(critExpected);
    harness.close();
  });

  it("showModal resets form controls", () => {
    const harness = buildHarness();
    inputElement(harness.document, "modal-id-input").value = "alice";
    buttonElement(harness.document, "modal-check-btn").disabled = false;
    buttonElement(harness.document, "modal-connect-btn").disabled = false;
    harness.api.setModalBadge("found", "found");
    harness.api.hideModal();
    harness.api.showModal();
    expect(classOf(harness.document, "session-modal")).toBe("modal-overlay");
    expect(inputElement(harness.document, "modal-id-input").value).toBe("");
    expect(buttonElement(harness.document, "modal-check-btn").disabled).toBe(true);
    expect(buttonElement(harness.document, "modal-connect-btn").disabled).toBe(true);
    expect(textOf(harness.document, "modal-connect-btn")).toBe("Connect");
    harness.close();
  });

  it("hideModal adds hidden class", () => {
    const harness = buildHarness();
    harness.api.hideModal();
    expect(classOf(harness.document, "session-modal")).toContain("hidden");
    harness.close();
  });

  it("modal input listener enables check button when text is present", () => {
    const harness = buildHarness();
    const input = inputElement(harness.document, "modal-id-input");
    const check = buttonElement(harness.document, "modal-check-btn");
    input.value = "alice";
    input.dispatchEvent(new harness.window.Event("input", { bubbles: true }));
    expect(check.disabled).toBe(false);
    harness.close();
  });

  it("modal input listener disables check button when text is empty", () => {
    const harness = buildHarness();
    const input = inputElement(harness.document, "modal-id-input");
    const check = buttonElement(harness.document, "modal-check-btn");
    input.value = "";
    input.dispatchEvent(new harness.window.Event("input", { bubbles: true }));
    expect(check.disabled).toBe(true);
    harness.close();
  });

  it("modal input listener resets badge and connect state", () => {
    const harness = buildHarness();
    const input = inputElement(harness.document, "modal-id-input");
    const connect = buttonElement(harness.document, "modal-connect-btn");
    harness.api.setModalBadge("found", "history");
    connect.disabled = false;
    input.value = "new-user";
    input.dispatchEvent(new harness.window.Event("input", { bubbles: true }));
    expect(classOf(harness.document, "modal-badge")).toBe("modal-badge");
    expect(connect.disabled).toBe(true);
    expect(textOf(harness.document, "modal-connect-btn")).toBe("Connect as new-user");
    harness.close();
  });

  it("space key toggles connect", () => {
    const harness = buildHarness();
    const spy = vi.spyOn(harness.api, "toggleConnect");
    harness.document.body.dispatchEvent(new harness.window.KeyboardEvent("keydown", { code: "Space", bubbles: true }));
    expect(spy).toHaveBeenCalled();
    harness.close();
  });
});

describe("demo.html candidate lookup and session actions", () => {
  it.each([
    ["Alice#1", "alice-1"],
    ["John Doe", "john-doe"],
    ["USER_42", "user_42"],
    ["--A--", "--a--"],
    ["a".repeat(80), "a".repeat(64)],
    ["  Mixed_Case-Id  ", "mixed_case-id"],
  ])("checkCandidateId sanitizes %j", async (rawValue, normalized) => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ exists: true, rounds: 1 }),
    });
    inputElement(harness.document, "modal-id-input").value = rawValue;
    await harness.api.checkCandidateId();
    expect(harness.fetchMock).toHaveBeenCalledWith(
      `/api/candidate/check?user_id=${encodeURIComponent(normalized)}`,
      { cache: "no-store" },
    );
    expect(textOf(harness.document, "modal-connect-btn")).toBe(`Connect as ${normalized}`);
    harness.close();
  });

  it("checkCandidateId shows resume badge for existing candidate", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ exists: true, rounds: 3 }),
    });
    inputElement(harness.document, "modal-id-input").value = "alice";
    await harness.api.checkCandidateId();
    expect(classOf(harness.document, "modal-badge")).toContain("found");
    expect(harness.document.getElementById("modal-badge")?.textContent).toContain("3 sessions in history");
    expect(buttonElement(harness.document, "modal-connect-btn").disabled).toBe(false);
    harness.close();
  });

  it("checkCandidateId shows new badge for fresh candidate", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ exists: false, rounds: 0 }),
    });
    inputElement(harness.document, "modal-id-input").value = "newbie";
    await harness.api.checkCandidateId();
    expect(classOf(harness.document, "modal-badge")).toContain("new");
    expect(harness.document.getElementById("modal-badge")?.textContent).toContain("starting fresh");
    harness.close();
  });

  it("checkCandidateId falls back to fresh start on fetch error", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockRejectedValue(new Error("network"));
    inputElement(harness.document, "modal-id-input").value = "broken";
    await harness.api.checkCandidateId();
    expect(classOf(harness.document, "modal-badge")).toContain("new");
    expect(harness.document.getElementById("modal-badge")?.textContent).toContain("Could not check history");
    expect(buttonElement(harness.document, "modal-connect-btn").disabled).toBe(false);
    harness.close();
  });

  it("checkCandidateId is a no-op for empty input", async () => {
    const harness = buildHarness();
    inputElement(harness.document, "modal-id-input").value = "   ";
    await harness.api.checkCandidateId();
    expect(harness.fetchMock).not.toHaveBeenCalled();
    harness.close();
  });

  it("startAnonymous hides modal and connects", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ livekit_url: "wss://lk", access_token: "token", room_name: "room-a" }),
    });
    await harness.api.startAnonymous();
    await flushAsyncWork();
    expect(classOf(harness.document, "session-modal")).toContain("hidden");
    expect(textOf(harness.document, "action-btn")).toBe("Disconnect");
    harness.close();
  });

  it("startAsCandidate does nothing until a candidate is selected", async () => {
    const harness = buildHarness();
    await harness.api.startAsCandidate();
    expect(harness.fetchMock).not.toHaveBeenCalled();
    harness.close();
  });

  it("startAsCandidate connects after candidate lookup", async () => {
    const harness = buildHarness();
    harness.fetchMock
      .mockResolvedValueOnce({ ok: true, json: async () => ({ exists: true, rounds: 2 }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ livekit_url: "wss://lk", access_token: "token", room_name: "room-b" }) });
    inputElement(harness.document, "modal-id-input").value = "alice";
    await harness.api.checkCandidateId();
    await harness.api.startAsCandidate();
    await flushAsyncWork();
    expect(textOf(harness.document, "user-name-badge")).toContain("candidate: alice");
    expect(textOf(harness.document, "action-btn")).toBe("Disconnect");
    harness.close();
  });
});

describe("demo.html transcript rendering", () => {
  it.each([
    ["user", "Hello there", "You"],
    ["agent", "Hi candidate", "Aura"],
    ["user", "x < y", "You"],
    ["agent", "say \"hi\"", "Aura"],
    ["user", "trim me   ", "You"],
    ["agent", "  padded  ", "Aura"],
  ])("addMsg(%s, %j)", (role, text, roleLabel) => {
    const harness = buildHarness();
    harness.api.addMsg(role, text);
    const messages = harness.document.querySelectorAll("#messages .msg");
    const last = messages[messages.length - 1]!;
    expect(last.className).toContain(role);
    expect(last.textContent).toContain(roleLabel);
    expect(last.innerHTML).not.toContain("<script");
    harness.close();
  });

  it("addMsg ignores empty content", () => {
    const harness = buildHarness();
    harness.api.addMsg("user", "   ");
    expect(harness.document.querySelectorAll("#messages .msg")).toHaveLength(0);
    harness.close();
  });

  it("showTyping adds a typing bubble", () => {
    const harness = buildHarness();
    harness.api.showTyping();
    expect(harness.document.getElementById("typing")).toBeTruthy();
    harness.close();
  });

  it("removeTyping removes the typing bubble", () => {
    const harness = buildHarness();
    harness.api.showTyping();
    harness.api.removeTyping();
    expect(harness.document.getElementById("typing")).toBeNull();
    harness.close();
  });

  it("showUserListening creates the listening bubble", () => {
    const harness = buildHarness();
    harness.api.showUserListening();
    expect(harness.document.getElementById("user-listening")).toBeTruthy();
    harness.close();
  });

  it("showUserInterim replaces the listening bubble", () => {
    const harness = buildHarness();
    harness.api.showUserListening();
    harness.api.showUserInterim("candidate answer");
    expect(harness.document.getElementById("user-listening")).toBeNull();
    expect(harness.document.getElementById("user-interim")?.textContent).toContain("candidate answer");
    harness.close();
  });

  it("clearUserInterim removes interim state", () => {
    const harness = buildHarness();
    harness.api.showUserInterim("candidate answer");
    harness.api.clearUserInterim();
    expect(harness.document.getElementById("user-interim")).toBeNull();
    harness.close();
  });

  it("showAgentInterim creates an interim bubble", () => {
    const harness = buildHarness();
    harness.api.showAgentInterim("thinking");
    expect(harness.document.getElementById("agent-interim")?.textContent).toContain("thinking");
    harness.close();
  });

  it("clearAgentInterim removes the interim bubble", () => {
    const harness = buildHarness();
    harness.api.showAgentInterim("thinking");
    harness.api.clearAgentInterim();
    expect(harness.document.getElementById("agent-interim")).toBeNull();
    harness.close();
  });

  it("upsertAgentFinal promotes interim content to a final bubble", () => {
    const harness = buildHarness();
    harness.api.showAgentInterim("final answer");
    harness.api.upsertAgentFinal("final answer");
    expect(harness.document.getElementById("agent-interim")).toBeNull();
    expect(harness.document.querySelector("#messages .msg.agent .bubble")?.textContent).toContain("final answer");
    harness.close();
  });

  it("upsertUserFinal promotes interim content to a final bubble", () => {
    const harness = buildHarness();
    harness.api.showUserInterim("candidate reply");
    harness.api.upsertUserFinal("candidate reply");
    expect(harness.document.getElementById("user-interim")).toBeNull();
    expect(harness.document.querySelector("#messages .msg.user .bubble")?.textContent).toContain("candidate reply");
    harness.close();
  });

  it.each([
    [{ text: "answer", interim: "", previous: "" }, "answer"],
    [{ text: "answer more", interim: "answer", previous: "" }, "answer more"],
    [{ text: "more", interim: "answer more", previous: "" }, "answer more"],
    [{ text: "answer more detail", interim: "answer more", previous: "answer" }, "more detail"],
    [{ text: "alpha beta", interim: "alpha", previous: "" }, "alpha beta"],
    [{ text: "beta", interim: "alpha beta", previous: "" }, "alpha beta"],
    [{ text: "same", interim: "same", previous: "" }, "same"],
    [{ text: "Hello WORLD", interim: "hello", previous: "Hello" }, "WORLD"],
    [{ text: "abc", interim: "", previous: "abc" }, "abc"],
    [{ text: "prefix suffix", interim: "", previous: "prefix" }, "suffix"],
    [{ text: "prefix", interim: "", previous: "prefix" }, "prefix"],
    [{ text: "  padded   text  ", interim: "  padded", previous: "" }, "padded text"],
  ])("reconcileUserTranscript %#", ({ text, interim, previous }, expected) => {
    const harness = buildHarness();
    harness.api.showUserInterim(interim);
    if (previous) {
      harness.api.upsertUserFinal(previous);
    }
    expect(harness.api.reconcileUserTranscript(text)).toBe(expected);
    harness.close();
  });

  it("hidePartial clears partial transcript", () => {
    const harness = buildHarness();
    const row = harness.document.getElementById("partial-row") as HTMLElement;
    row.style.display = "flex";
    harness.document.getElementById("partial-text")!.textContent = "partial";
    harness.api.hidePartial();
    expect(row.style.display).toBe("none");
    expect(textOf(harness.document, "partial-text")).toBe("");
    harness.close();
  });

  it("clearTranscript restores the empty state", () => {
    const harness = buildHarness();
    harness.api.addMsg("user", "hello");
    harness.api.clearTranscript();
    expect(harness.document.getElementById("empty-state")).toBeTruthy();
    expect(harness.document.querySelectorAll("#messages .msg")).toHaveLength(0);
    harness.close();
  });
});

describe("demo.html summary rendering", () => {
  it.each([
    ["customer_ended_call", "ended by candidate"],
    ["assistant_ended_call", "ended by Aura"],
    ["exceeded_max_duration", "max duration reached"],
    ["silence_timed_out", "idle timeout"],
    ["custom_reason", "custom_reason"],
    [undefined, ""],
  ])("showSummary reason mapping %#", (reason, expected) => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 65,
      ended_reason: reason,
      rubric_grades: {},
      answer_notes: [],
      questions_asked: [],
    });
    expect(textOf(harness.document, "summary-ended-reason")).toBe(expected);
    harness.close();
  });

  it.each([
    [{ communication: { grade: "strong", notes: "Clear." } }, "communication · Strong"],
    [{ system_design: { grade: "weak", notes: "Needs structure." } }, "system_design · Weak"],
    [{ problem_solving: { grade: "mixed", notes: "Partial." } }, "problem_solving · Mixed"],
  ])("showSummary renders grade chips %#", (grades, expected) => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 90,
      rubric_grades: grades,
      answer_notes: [],
      questions_asked: [],
    });
    expect(harness.document.getElementById("summary-body")?.textContent).toContain(expected);
    harness.close();
  });

  it.each([
    [[{ question: "Q1", strength: "Good", weakness: "Slow" }], "Q1"],
    [[{ question: "Q2", strength: "Clear", weakness: "None" }], "Q2"],
    [[{ question: "Q3", strength: "Accurate", weakness: "Shallow" }], "Q3"],
  ])("showSummary renders answer notes %#", (notes, expected) => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 90,
      rubric_grades: {},
      answer_notes: notes,
      questions_asked: [],
    });
    expect(harness.document.getElementById("summary-body")?.textContent).toContain(expected);
    harness.close();
  });

  it.each([
    [["Q1"], "Q1"],
    [["Q1", "Q2"], "Q2"],
    [["Graph", "DP", "Tree"], "Tree"],
  ])("showSummary renders questions %#", (questions, expected) => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 90,
      rubric_grades: {},
      answer_notes: [],
      questions_asked: questions,
    });
    expect(harness.document.getElementById("summary-body")?.textContent).toContain(expected);
    harness.close();
  });

  it.each([
    ["Strong decomposition.", "Strong decomposition."],
    ["Needs better edge-case handling.", "Needs better edge-case handling."],
    ["Clear and concise.", "Clear and concise."],
  ])("showSummary renders narrative summary %#", (summaryText, expected) => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 90,
      narrative_summary: summaryText,
      rubric_grades: {},
      answer_notes: [],
      questions_asked: [],
    });
    expect(harness.document.getElementById("summary-body")?.textContent).toContain(expected);
    harness.close();
  });

  it("showSummary opens the summary overlay", () => {
    const harness = buildHarness();
    harness.api.showSummary({ duration_secs: 125, rubric_grades: {}, answer_notes: [], questions_asked: [] });
    expect(classOf(harness.document, "summary-overlay")).toBe("modal-overlay");
    expect(textOf(harness.document, "summary-duration")).toBe("2m 5s");
    harness.close();
  });

  it("closeSummary hides summary and reopens modal", () => {
    const harness = buildHarness();
    harness.api.showSummary({ duration_secs: 125, rubric_grades: {}, answer_notes: [], questions_asked: [] });
    harness.api.closeSummary();
    expect(classOf(harness.document, "summary-overlay")).toContain("hidden");
    expect(classOf(harness.document, "session-modal")).toBe("modal-overlay");
    harness.close();
  });

  it("downloadSummary creates and revokes a blob URL", () => {
    const harness = buildHarness();
    harness.api.showSummary({
      duration_secs: 125,
      ended_reason: "customer_ended_call",
      narrative_summary: "Strong problem decomposition.",
      rubric_grades: {},
      answer_notes: [],
      questions_asked: [],
    });
    harness.api.downloadSummary();
    expect(harness.createObjectUrlMock).toHaveBeenCalled();
    expect(harness.clickMock).toHaveBeenCalled();
    expect(harness.revokeObjectUrlMock).toHaveBeenCalledWith("blob:demo");
    harness.close();
  });

  it("_showSummaryLoading reveals the spinner", () => {
    const harness = buildHarness();
    harness.api._showSummaryLoading();
    expect(classOf(harness.document, "summary-loading-overlay")).toBe("modal-overlay");
    harness.close();
  });

  it("_hideSummaryLoading hides the spinner", () => {
    const harness = buildHarness();
    harness.api._showSummaryLoading();
    harness.api._hideSummaryLoading();
    expect(classOf(harness.document, "summary-loading-overlay")).toContain("hidden");
    harness.close();
  });
});

describe("demo.html server event handling", () => {
  it.each([
    [{ type: "status", message: "ready soon" }, ["state-label", "ready soon"]],
    [{ type: "user-started-speaking" }, ["pill-text", "listening"]],
    [{ type: "user-stopped-speaking" }, ["cd-idle", "cd-badge"]],
    [{ type: "bot-stopped-speaking" }, ["pill-text", "idle"]],
    [{ type: "bot-llm-text", data: { text: "Draft reply" } }, ["messages", "Draft reply"]],
    [{ type: "bot-transcription", data: { text: "Final reply", final: true } }, ["messages", "Final reply"]],
    [{ type: "latency", data: { total_ms: 210 } }, ["m-sts", "210"]],
    [{ type: "metrics", data: { tokens: [{ prompt_tokens: 10, completion_tokens: 5 }] } }, ["m-tok-session", "15"]],
    [{ type: "generating-summary" }, ["state-label", "generating your feedback…"]],
    [{ type: "user-identity-set", data: { user_id: "alice", name: "Alice" } }, ["user-name-badge", "candidate: alice"]],
  ])("handleServerEvent basic branch %#", async (message, [id, expected]) => {
    const harness = buildHarness();
    await harness.api.handleServerEvent(message);
    const actual = message.type === "user-stopped-speaking"
      ? classOf(harness.document, id)
      : textOf(harness.document, id);
    expect(actual).toContain(expected);
    harness.close();
  });

  it("handleServerEvent session-config starts countdowns", async () => {
    vi.useFakeTimers();
    const harness = buildHarness();
    await harness.api.handleServerEvent({
      type: "session-config",
      data: { idle_timeout_secs: 90, max_duration_secs: 600 },
    });
    expect(classOf(harness.document, "countdown-row")).toContain("visible");
    vi.advanceTimersByTime(1000);
    expect(textOf(harness.document, "cd-session-val")).toBe("9:59");
    harness.close();
  });

  it("handleServerEvent interruption suppresses audio and listening state", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "interruption" });
    expect(harness.pauseMock).toHaveBeenCalled();
    expect(textOf(harness.document, "pill-text")).toBe("listening");
    harness.close();
  });

  it("handleServerEvent user-transcription interim renders user interim", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "user-transcription", data: { text: "partial answer", final: false } });
    expect(harness.document.getElementById("user-interim")?.textContent).toContain("partial answer");
    harness.close();
  });

  it("handleServerEvent user-transcription final promotes to final user bubble", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "user-transcription", data: { text: "partial answer", final: false } });
    await harness.api.handleServerEvent({ type: "user-transcription", data: { text: "partial answer complete", final: true } });
    expect(harness.document.querySelector("#messages .msg.user .bubble")?.textContent).toContain("partial answer complete");
    harness.close();
  });

  it("handleServerEvent bot-started-speaking plays audio and switches state", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "bot-started-speaking" });
    expect(harness.playMock).toHaveBeenCalled();
    expect(textOf(harness.document, "pill-text")).toBe("speaking");
    harness.close();
  });

  it("handleServerEvent latency-breakdown renders breakdown rows", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({
      type: "latency-breakdown",
      data: { events: ["User turn: 0.601s", "AzureRealtimeLLMService#0: TTFB 0.536s"] },
    });
    expect(harness.document.getElementById("m-breakdown")?.innerHTML).toContain("Turn wait");
    expect(harness.document.getElementById("m-breakdown")?.innerHTML).toContain("601ms");
    harness.close();
  });

  it("handleServerEvent call-summary shows summary immediately when disconnected", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "bot-stopped-speaking" });
    await harness.api.handleServerEvent({
      type: "call-summary",
      data: { duration_secs: 80, rubric_grades: {}, answer_notes: [], questions_asked: [] },
    });
    expect(classOf(harness.document, "summary-overlay")).toBe("modal-overlay");
    harness.close();
  });

  it("handleServerEvent ignores unknown messages", async () => {
    const harness = buildHarness();
    await harness.api.handleServerEvent({ type: "unknown-event" });
    expect(textOf(harness.document, "state-label")).toBe("press connect to start");
    harness.close();
  });
});

describe("demo.html connection and support", () => {
  it.each([
    [false, { getUserMedia: vi.fn() }, "Microphone access requires HTTPS or localhost"],
    [true, undefined, "This browser cannot access the microphone here. Use HTTPS or localhost."],
    [true, {}, "This browser cannot access the microphone here. Use HTTPS or localhost."],
    [true, { getUserMedia: vi.fn() }, null],
    [true, { getUserMedia: () => undefined }, null],
    [true, { getUserMedia: vi.fn().mockResolvedValue(undefined) }, null],
  ])("getMicrophoneSupportError %#", (isSecure, mediaDevices, expected) => {
    const harness = buildHarness();
    Object.defineProperty(harness.window, "isSecureContext", { configurable: true, value: isSecure });
    Object.defineProperty(harness.window.navigator, "mediaDevices", { configurable: true, value: mediaDevices });
    expect(harness.api.getMicrophoneSupportError()).toBe(expected);
    harness.close();
  });

  it("connect handles unsupported microphone access", async () => {
    const harness = buildHarness();
    Object.defineProperty(harness.window, "isSecureContext", { configurable: true, value: false });
    await harness.api.connect();
    expect(textOf(harness.document, "pill-text")).toBe("error");
    expect(textOf(harness.document, "state-label")).toContain("Microphone access requires HTTPS");
    harness.close();
  });

  it("connect creates a room and bootstraps a session", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ livekit_url: "wss://lk", access_token: "token", room_name: "room-1" }),
    });
    await harness.api.connect();
    expect(harness.createdRooms).toHaveLength(1);
    expect(harness.fetchMock).toHaveBeenCalledWith("/api/livekit/session", expect.objectContaining({ method: "POST" }));
    expect(textOf(harness.document, "action-btn")).toBe("Disconnect");
    harness.close();
  });

  it("connect handles bootstrap failure", async () => {
    const harness = buildHarness();
    harness.fetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    await harness.api.connect();
    expect(textOf(harness.document, "action-btn")).toBe("Connect");
    expect(textOf(harness.document, "pill-text")).toBe("error");
    harness.close();
  });

  it("toggleConnect shows modal from idle state", () => {
    const harness = buildHarness();
    harness.api.toggleConnect();
    expect(classOf(harness.document, "session-modal")).toBe("modal-overlay");
    harness.close();
  });

  it("window beforeunload triggers cleanup", () => {
    const harness = buildHarness();
    const spy = vi.spyOn(harness.api, "cleanup");
    harness.window.dispatchEvent(new harness.window.Event("beforeunload"));
    expect(spy).toHaveBeenCalled();
    harness.close();
  });
});