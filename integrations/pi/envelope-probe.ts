/**
 * Envelope probe — loaded ONLY by zicato's envelope test, never by a run.
 *
 * Writes the agent's active tool list to the file named by
 * `ZICATO_PI_ENVELOPE_PROBE` as soon as the session starts, so the test can
 * assert what the model can actually call: the sanctioned set and nothing
 * else — no `bash`/`read`/`grep` builtins, no pi skills, no memory packages.
 * Registers no tools of its own, so its presence does not widen the set it
 * reports.
 */

import { writeFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const out = process.env.ZICATO_PI_ENVELOPE_PROBE;
	if (!out) return;
	pi.on("session_start", () => {
		writeFileSync(out, `${JSON.stringify({ tools: [...pi.getActiveTools()].sort() })}\n`, "utf-8");
	});
}
