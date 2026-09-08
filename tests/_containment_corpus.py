"""Construct shared containment cases with real mutation and patch semantics."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from tests._workspace_support import experiment_record
from zicato.core.mutation import Patch
from zicato.epoch.containment import build_manifest, write_mutation_policy
from zicato.epoch.journal import patch_body
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy, source_files

CORPUS = Path(__file__).parent / "fixtures" / "mutation_containment.json"


def _tree(root: Path) -> dict[str, dict[str, Any]]:
    return {
        entry.path: {"hex": (root / entry.path).read_bytes().hex(), "executable": entry.executable}
        for entry in source_files(root)
    }


def _case(
    name: str,
    filename: str,
    source: bytes,
    replacement: str,
    *,
    mutation_id: str = "unit",
    forbidden: tuple[str, ...] = (),
    child_source: bytes | None = None,
    extra: str = "",
    expected: str = "contained",
    op: str = "replace",
    numeric: float = 0.42,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="zi-corpus-") as directory:
        root = Path(directory)
        parent = root / "parent"
        parent.mkdir()
        (parent / filename).write_bytes(source)
        (parent / "opaque.bin").write_bytes(b"\xff\x00")
        policy = MutationPolicy.capture(parent, enumerate_mutations([parent]), forbidden)
        patch = Patch(
            "patch",
            mutation_id,
            op,
            replacement if op == "replace" else None,
            numeric if op == "set_numeric" else None,
            "concise" if op == "set_enum" else None,
            "Change the declared unit.",
        )
        child = root / "child"
        apply_patches(parent, [patch], child)
        if child_source is not None:
            (child / filename).write_bytes(child_source)
        if extra == "add":
            (child / "added.bin").write_bytes(b"added")
        elif extra == "delete":
            (child / "opaque.bin").unlink()
        elif extra == "executable":
            (child / filename).chmod(0o755)
        patch_records = {
            patch.id: (json.dumps(patch_body(patch), indent=2, sort_keys=True) + "\n").encode()
        }
        contract = root / "epochs" / "epoch"
        contract.mkdir(parents=True)
        brief = "# Proposer brief\n## Forbidden edits\n" + "".join(
            f"- `{mid}`\n" for mid in forbidden
        )
        (contract / "brief.md").write_text(brief)
        (contract / "scoring.json").write_text("{}\n")
        policy_sha256 = write_mutation_policy(
            root, epoch_id="epoch", parent_generation_id="v0", policy=policy
        )
        policy_bytes = (
            contract / "generations" / "v0" / "mutation-policies" / f"{policy_sha256}.json"
        ).read_bytes()
        manifest = build_manifest(
            policy,
            child,
            workspace_root=root,
            epoch_id="epoch",
            parent_generation_id="v0",
            generation_id="v1",
            patches=[patch],
            patch_records=patch_records,
            policy_sha256=policy_sha256,
        )
        return {
            "name": name,
            "expected_status": expected,
            "parent": _tree(parent),
            "child": _tree(child),
            "manifest": json.loads(json.dumps(manifest.to_dict())),
            "patch_records": {key: value.hex() for key, value in patch_records.items()},
            "experiment": {
                "epoch_id": "epoch",
                "parent_generation_id": "v0",
                "generation_id": "v1",
                "patch_ids": ["patch"],
            },
            "forbidden_ids": list(forbidden),
            "policy": policy_bytes.hex(),
            "brief": brief.encode().hex(),
            "scoring": b"{}\n".hex(),
        }


def build_cases() -> dict[str, Any]:
    literal = b'# zicato:mutable id="unit"\nPROMPT = "original"\nTAIL = 1\n'
    code = (
        b'# zicato:mutable:code id="unit"\nVALUE = 1\nSECOND = 2\n# zicato:mutable:end\nTAIL = 3\n'
    )
    cases = [
        _case("literal", "prompt.py", literal, '"edited"'),
        _case("numeric-operation", "prompt.py", literal, "", op="set_numeric"),
        _case(
            "negative-numeric-operation", "prompt.py", literal, "", op="set_numeric", numeric=-0.42
        ),
        _case("enum-operation", "prompt.py", literal, "", op="set_enum"),
        _case("literal-crlf", "prompt.py", literal.replace(b"\n", b"\r\n"), '"edited"'),
        _case(
            "literal-unicode",
            "prompt.py",
            literal.replace(b"PROMPT", "PRÖMPT".encode()),
            '"edited"',
        ),
        _case(
            "assignment-rename",
            "prompt.py",
            literal,
            '"edited"',
            child_source=literal.replace(b"PROMPT", b"RENAMED").replace(b"original", b"edited"),
            expected="violated",
        ),
        _case(
            "rename-with-unchanged-literal",
            "prompt.py",
            literal,
            '"original"',
            child_source=literal.replace(b"PROMPT", b"RENAMED"),
            expected="violated",
        ),
        _case(
            "adjacent-expression",
            "prompt.py",
            literal,
            '"edited"',
            child_source=literal.replace(b'"original"', b'"edited"; EXTRA = 1'),
            expected="violated",
        ),
        _case("code-insertion-at-start", "code.py", code, "ADDED = 0\nVALUE = 1\nSECOND = 2\n"),
        _case("code-insertion-at-end", "code.py", code, "VALUE = 1\nSECOND = 2\nADDED = 3\n"),
        _case("code-deletion", "code.py", code, "VALUE = 1\n"),
        _case(
            "before-code-marker",
            "code.py",
            code,
            "VALUE = 4\n",
            child_source=b"EXTRA = 0\n" + code,
            expected="violated",
        ),
        _case(
            "forbidden-literal",
            "prompt.py",
            literal,
            '"edited"',
            forbidden=("unit",),
            expected="violated",
        ),
        _case("created-file", "prompt.py", literal, '"edited"', extra="add", expected="violated"),
        _case(
            "deleted-file", "prompt.py", literal, '"edited"', extra="delete", expected="violated"
        ),
        _case(
            "executable-change",
            "prompt.py",
            literal,
            '"edited"',
            extra="executable",
            expected="violated",
        ),
    ]
    for filename, marker, content, replacement in (
        ("module.py", '# zicato:mutable:file id="unit"\n', "VALUE = 1\n", "RENAMED = 2\n"),
        ("brief.md", '<!-- zicato:mutable:file id="unit" -->\n', "original\n", "edited\n"),
        (
            "config.yaml",
            '# zicato:mutable:file id="unit"\n',
            "value: original\n",
            "value: edited\n",
        ),
        (
            "config.toml",
            '# zicato:mutable:file id="unit"\n',
            'value = "original"\n',
            'value = "edited"\n',
        ),
    ):
        cases.append(_case(filename, filename, (marker + content).encode(), marker + replacement))
    nested = (
        b'# zicato:mutable:file id="unit"\n# zicato:mutable id="protected"\n'
        b'VALUE = "original"\nOTHER = 1\n'
    )
    cases.append(
        _case(
            "file-edits-protected-numeric",
            "module.py",
            nested,
            nested.replace(b"OTHER = 1", b"OTHER = 2").decode(),
            forbidden=("protected",),
            expected="violated",
        )
    )
    protected_literal = nested.replace(b"OTHER = 1", b'OTHER = "first"')
    cases.append(
        _case(
            "file-removes-unprotected-inner-unit",
            "module.py",
            nested,
            '# zicato:mutable:file id="unit"\nOTHER = "retained"\n',
        )
    )
    cases.append(
        _case(
            "file-removes-protected-inner-unit",
            "module.py",
            nested,
            '# zicato:mutable:file id="unit"\nOTHER = "retained"\n',
            forbidden=("protected",),
            expected="violated",
        )
    )
    cases.extend(
        [
            _case(
                "file-with-protected-literal",
                "module.py",
                protected_literal,
                protected_literal.replace(b'OTHER = "first"', b'OTHER = "second"').decode(),
                forbidden=("protected",),
            ),
            _case(
                "file-edits-protected-literal",
                "module.py",
                nested,
                nested.replace(b"original", b"edited").decode(),
                forbidden=("protected",),
                expected="violated",
            ),
        ]
    )
    for corruption in (
        "missing-manifest",
        "missing-span",
        "wrong-child-location",
        "wrong-epoch",
        "wrong-child-hash",
        "extra-observed-file",
        "patch-mismatch",
        "negative-byte-offset",
    ):
        case = copy.deepcopy(cases[0])
        case["name"] = corruption
        case["expected_status"] = "unverified"
        match corruption:
            case "missing-manifest":
                case["manifest"] = None
            case "missing-span":
                for record in case["manifest"]["files"]:
                    record["spans"] = []
            case "wrong-child-location":
                case["manifest"]["child_source"] = "parent"
            case "wrong-epoch":
                case["manifest"]["epoch_id"] = "another-epoch"
            case "wrong-child-hash":
                case["manifest"]["files"][0]["child_sha256"] = "0" * 64
            case "extra-observed-file":
                case["child"]["unrecorded.bin"] = {"hex": "ff", "executable": False}
            case "patch-mismatch":
                case["patch_records"]["patch"] = b"{}".hex()
            case "negative-byte-offset":
                next(record for record in case["manifest"]["files"] if record["changes"])[
                    "changes"
                ][0]["parent_start"] = -1
        cases.append(case)
    for corruption in (
        "policy-parent",
        "policy-inventory",
        "policy-metadata",
        "policy-forbidden",
        "policy-escaping-root",
        "policy-unselected-point",
    ):
        case = copy.deepcopy(cases[0])
        case.update(name=corruption, expected_status="unverified")
        policy_body = json.loads(bytes.fromhex(case["policy"]))
        match corruption:
            case "policy-parent":
                policy_body["parent_generation_id"] = "v2"
            case "policy-inventory":
                policy_body["source_files"].pop()
            case "policy-metadata":
                policy_body["points"][0]["metadata"] = [["other", "value"]]
            case "policy-forbidden":
                policy_body["points"][0]["forbidden"] = True
            case "policy-escaping-root":
                policy_body["enumeration_roots"] = [".."]
            case "policy-unselected-point":
                policy_body["enumeration_roots"] = ["opaque.bin"]
        data = (json.dumps(policy_body, sort_keys=True, indent=2) + "\n").encode()
        case["policy"] = data.hex()
        case["manifest"]["policy_sha256"] = hashlib.sha256(data).hexdigest()
        cases.append(case)
    vacuous = copy.deepcopy(cases[0])
    vacuous.update(
        name="absent-source-and-mutation-evidence",
        expected_status="unverified",
        parent={},
        child={},
        patch_records={},
    )
    vacuous["manifest"]["files"] = []
    vacuous["manifest"]["patches"] = []
    vacuous["experiment"]["patch_ids"] = []
    cases.append(vacuous)
    for corruption in (
        "missing-policy",
        "corrupt-policy",
        "changed-brief",
        "changed-scoring",
        "altered-forbidden-flags",
    ):
        original = (
            next(case for case in cases if case["name"] == "forbidden-literal")
            if corruption == "altered-forbidden-flags"
            else cases[0]
        )
        case = copy.deepcopy(original)
        case.update(name=corruption, expected_status="unverified")
        match corruption:
            case "missing-policy":
                case["policy"] = None
            case "corrupt-policy":
                case["policy"] += "20"
            case "changed-brief":
                case["brief"] = b"# Changed permissions\n".hex()
            case "changed-scoring":
                case["scoring"] = b'{"mutation_syntax": {}}\n'.hex()
            case "altered-forbidden-flags":
                for record in case["manifest"]["files"]:
                    for span in record["spans"]:
                        span["forbidden"] = False
        cases.append(case)
    return {"format_version": 1, "cases": cases}


def materialize_case(case: dict[str, Any], root: Path) -> Path:
    for role in ("parent", "child"):
        (root / role).mkdir(parents=True)
        for relative, value in case[role].items():
            path = root / role / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes.fromhex(value["hex"]))
            path.chmod(0o755 if value["executable"] else 0o644)
    epoch = root / "epochs" / "epoch"
    epoch.mkdir(parents=True, exist_ok=True)
    (epoch / "brief.md").write_bytes(bytes.fromhex(case["brief"]))
    (epoch / "scoring.json").write_bytes(bytes.fromhex(case["scoring"]))
    if (
        case.get("policy") is not None
        and case["manifest"] is not None
        and case["manifest"].get("policy_sha256")
    ):
        policy = (
            epoch
            / "generations"
            / "v0"
            / "mutation-policies"
            / f"{case['manifest']['policy_sha256']}.json"
        )
        policy.parent.mkdir(parents=True)
        policy.write_bytes(bytes.fromhex(case["policy"]))
    generation = root / "epochs" / "epoch" / "generations" / "v1"
    (generation / "patches").mkdir(parents=True)
    (generation / "experiment.json").write_text(json.dumps(experiment_record(**case["experiment"])))
    if case["manifest"] is not None:
        (generation / "containment.json").write_text(json.dumps(case["manifest"]))
    for key, value in case["patch_records"].items():
        (generation / "patches" / f"{key}.json").write_bytes(bytes.fromhex(value))
    return generation
