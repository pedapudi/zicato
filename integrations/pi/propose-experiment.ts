/**
 * zicato's terminating structured-output tool for the external proposer.
 *
 * The proposer's ONLY sanctioned way to end a turn. Its typebox schema
 * mirrors `EXPERIMENT_JSON_SCHEMA` in `src/zicato/proposer/structured.py`,
 * so a shape mismatch is caught at the tool-call layer and the model
 * repairs it before Python ever sees the payload. `terminate: true` ends
 * the turn on the tool call itself, without paying for a follow-up LLM
 * round-trip.
 *
 * The tool is deliberately inert: it validates and acknowledges. zicato
 * reads the emitted arguments off the RPC `tool_execution_start` event and
 * runs `parse_experiment_json` over them — that cross-check stays
 * authoritative (mutation-id resolution, op/new_* discrimination, min/max
 * and enum domains, registered drift kinds), because nothing in this file
 * can see the mutation manifest.
 *
 * Keep the field names in step with the Python schema: the tool contract
 * is the proposer's causal surface, and `tests/test_proposer_pi_envelope.py`
 * fails when a schema property has no counterpart here.
 */

import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const directionEnum = Type.Union(
	[
		Type.Literal("decrease"),
		Type.Literal("increase"),
		Type.Literal("neutral"),
		Type.Literal("decrease_or_neutral"),
		Type.Literal("increase_or_neutral"),
	],
	{ description: "Direction the metric is expected to move" },
);

const magnitudeEnum = Type.Union([Type.Literal("small"), Type.Literal("medium"), Type.Literal("large")], {
	description: "Expected size of the move",
});

const hypothesisSchema = Type.Object({
	core_idea: Type.String({ description: "The change, in one sentence" }),
	modulating: Type.Array(Type.String(), {
		description: "Mutation ids this experiment modulates, verbatim from the manifest",
		minItems: 1,
	}),
	why: Type.String({ description: "Why this change should move the metric you predict" }),
	expected_pass_rate_delta: Type.String({ description: 'Predicted pass-rate move, e.g. "+0.05"' }),
	expected_drift_movements: Type.Optional(
		Type.Array(
			Type.Object({
				kind: Type.String({ description: "Registered drift kind" }),
				direction: directionEnum,
				magnitude: magnitudeEnum,
			}),
			{ description: "Predicted drift-kind movements" },
		),
	),
	expected_metric_movements: Type.Optional(
		Type.Array(
			Type.Object({
				metric_name: Type.String({ description: "Metric or judge name" }),
				direction: directionEnum,
				magnitude: magnitudeEnum,
			}),
			{ description: "Predicted named-metric movements" },
		),
	),
	risks: Type.Optional(Type.String({ description: "What could go wrong with this change" })),
});

const patchSchema = Type.Object({
	mutation_id: Type.String({ description: "Id of the mutation point being patched, verbatim from the manifest" }),
	op: Type.Union([Type.Literal("replace"), Type.Literal("set_numeric"), Type.Literal("set_enum")], {
		description: "Patch operation; it must match the mutation point's kind",
	}),
	new_content: Type.Optional(Type.String({ description: "Replacement content for a replace patch" })),
	new_numeric: Type.Optional(Type.Number({ description: "Replacement value for a set_numeric patch" })),
	new_enum: Type.Optional(Type.String({ description: "Replacement member for a set_enum patch" })),
	rationale: Type.String({ description: "Why this specific edit" }),
});

const proposeExperiment = defineTool({
	name: "propose_experiment",
	label: "Propose Experiment",
	description:
		"Emit the finished experiment: one hypothesis plus the patch set that tests it. This is your final action — call it exactly once, when the patch set is complete and you believe it valid.",
	promptSnippet: "Emit the finished experiment as a terminating tool result",
	promptGuidelines: [
		"propose_experiment is the only way to finish. Call it exactly once, as your last action.",
		"Every mutation_id must appear verbatim in the manifest you were given.",
		"After calling propose_experiment, do not emit another assistant response in the same turn.",
	],
	parameters: Type.Object({
		hypothesis: hypothesisSchema,
		patches: Type.Array(patchSchema, { description: "The edits, in application order", minItems: 1 }),
	}),

	async execute(_toolCallId, params) {
		const patches = Array.isArray(params.patches) ? params.patches.length : 0;
		return {
			content: [{ type: "text", text: `Experiment recorded: ${patches} patch(es).` }],
			terminate: true,
		};
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(proposeExperiment);
}
