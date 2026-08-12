import { liveRuntimeBlockers } from "./live-runtime";

export default async function liveGlobalSetup() {
  const blockers = await liveRuntimeBlockers();
  if (blockers.length === 0) return;

  const message = `BLOCKED: ${blockers.join("; ")}`;
  console.log(message);
  if (process.env.WIKIPEDIARAG_REQUIRE_LIVE_E2E === "1")
    throw new Error(message);
}
