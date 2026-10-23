import hashlib
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from factory.usage_collector import (collect_event, collect_future_source_file, collect_rollout, collect_source_file, estimate_groups,
                                     api_price_proxy, expected_cost_per_accepted, quota_interval, read_records)

# The development collector owns template data; installed adapters own project data.
from factory import usage_collector as _collector
ROOT = _collector.ROOT
(ROOT / "work/factory-tests/collector").mkdir(parents=True, exist_ok=True)
THREAD = "0123456789abcdef"
PARENT_SESSION = "fedcba9876543210"


def line(path, value, newline=True):
    with path.open("ab") as f:
        f.write(json.dumps(value).encode() + (b"\n" if newline else b""))


def context(turn="turn-a", model="gpt-6-luna", effort="medium"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model, "effort": effort}}


def session_meta(thread=THREAD):
    return {"type": "session_meta", "payload": {"id": thread, "session_id": thread}}


def usage(turn, response, n, thread=THREAD, session=None):
    counters = {"input_tokens": n, "cached_input_tokens": n // 2, "cache_write_input_tokens": 0,
                "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": n + 2}
    return {"type": "token_usage_record", "timestamp": "2026-10-19T00:00:00Z",
            "payload": {"thread_id": thread, "session_id": session or thread, "turn_id": turn,
                        "response_id": response, "usage": counters,
                        "turn_token_usage": counters, "thread_token_usage": counters}}


def fresh_timestamp(seconds=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def future_manifest(stream):
    stat = stream.stat()
    with stream.open("rb") as f:
        header = f.readline(65537)
    return {"type": "source", "collection_mode": "future_only", "source_id": THREAD,
            "thread_id": THREAD, "session_id": THREAD, "task_id": "future-test",
            "path": str(stream), "task_class": "coordinator-overhead",
            "baseline": {"device": stat.st_dev, "inode": stat.st_ino,
                         "observed_size": stat.st_size,
                         "header_sha256": hashlib.sha256(header).hexdigest()}}


class NativeCallAuditTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from factory import native_hook
        self.hook = native_hook
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work/factory-tests/collector")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.home = self.base / "codex"
        self.home.mkdir()
        self.thread = "019a1234-1234-7123-8123-123456789abc"
        self.call = "call_original"
        self.preparation = self.root / ("work/task/jev-" + "a" * 32 + ".json")
        self.preparation.parent.mkdir(parents=True)
        self.packet = {"task_name": "cpi414_carrier", "message": "private original task",
                       "model": "gpt-6-luna", "reasoning_effort": "high", "fork_turns": "none"}
        self.prepared = {"native_request": self.packet, "capability_captured_at": 1792454400,
                         "capability_expires_at": 1792454460}
        self.write_preparation()
        self.rollout = self.home / "sessions/2026/10/20" / ("rollout-date-" + self.thread + ".jsonl")
        self.rollout.parent.mkdir(parents=True)
        self.database = self.home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?, ?)",
                               (self.thread, str(self.rollout), str(self.root)))
        self.meta = {"type": "session_meta", "payload": {"id": self.thread, "cwd": str(self.root)}}
        self.function = {"type": "response_item", "timestamp": "2026-10-20T00:00:30Z",
                         "payload": {"type": "function_call", "call_id": self.call,
                                     "name": "collaboration.spawn_agent", "arguments": json.dumps(self.packet)}}
        self.output = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": self.call,
                                                          "output": json.dumps({"task_name": "/root/worker"})}}
        self.write_rollout()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        from factory import consumer
        self.shared = patch.object(consumer, "shared_root", side_effect=lambda p: p)
        self.shared.start(); self.addCleanup(self.shared.stop)
        self.validation = patch.object(self.hook, "validate", return_value=(None, None, None, self.preparation, None, None))
        self.validator = self.validation.start(); self.addCleanup(self.validation.stop)

    def write_preparation(self):
        self.preparation.write_text(json.dumps(self.prepared))
        self.preparation.chmod(0o600)

    def write_rollout(self, rows=None):
        self.rollout.write_text("")
        for row in rows if rows is not None else [self.meta, self.function, self.output]:
            line(self.rollout, row)

    def audit(self):
        return _collector.audit_native_call(self.root, str(self.preparation), self.thread, self.call)

    def test_original_match_is_retrospective_and_read_only(self):
        before = {p: p.read_bytes() for p in [self.preparation, self.rollout, self.database]}
        result = self.audit()
        self.assertEqual(result["status"], "matching_reported_launch")
        self.assertTrue(result["fresh_at_call"])
        self.assertFalse(result["replay_authorized"])
        self.assertEqual(result["caller_acceptance"], "not_performed")
        self.assertEqual(result["terminal_result"], "unverified")
        self.assertEqual(result["reported_handle_sha256"], hashlib.sha256(b"/root/worker").hexdigest())
        self.assertNotIn("private original", json.dumps(result))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.validator.assert_called_once()
        self.assertEqual(self.validator.call_args.kwargs, {"fresh": False})

    def test_incident_actual_arguments_override_fabricated_consumption(self):
        actual = dict(self.packet, task_name="changed_task", message="changed private task")
        self.function["payload"]["arguments"] = json.dumps(actual)
        self.output["payload"]["output"] = "Native worker preparation refused: native_plaintext_marker_required"
        self.write_rollout()
        self.preparation.with_suffix(".consume.json").write_text(json.dumps({"arguments": self.packet}))
        result = self.audit()
        self.assertEqual(result["status"], "argument_mismatch")
        self.assertEqual(result["mismatched_fields"], ["message", "task_name"])
        self.assertEqual(result["native_result"], "native_refused")
        self.assertNotIn("changed private", json.dumps(result))

    def test_matching_expired_refusal_and_historical_reported_launch(self):
        self.function["timestamp"] = "2026-10-20T00:02:00Z"
        self.output["payload"]["output"] = "Native worker preparation refused: native_capability_expired"
        self.write_rollout()
        result = self.audit()
        self.assertEqual(result["status"], "matching_without_launch")
        self.assertFalse(result["fresh_at_call"])
        self.assertEqual(result["refusal_reason"], "native_capability_expired")
        self.output["payload"]["output"] = json.dumps({"agent_id": "worker-123"})
        self.write_rollout()
        result = self.audit()
        self.assertEqual(result["status"], "matching_reported_launch")
        self.assertFalse(result["fresh_at_call"])
        self.assertFalse(result["replay_authorized"])

    def test_missing_duplicate_and_malformed_records_refuse(self):
        for rows, reason in [([self.meta, self.function], "missing_result"),
                             ([self.meta, self.function, self.function, self.output], "call_missing_or_duplicate"),
                             ([self.meta, self.function, self.output, self.output], "result_duplicate"),
                             ([self.meta, self.output, self.function], "result_precedes_call"),
                             ([self.meta, self.output], "call_missing_or_duplicate"),
                             ([self.function, self.output], "session_metadata_mismatch")]:
            with self.subTest(reason=reason):
                self.write_rollout(rows)
                result = self.audit()
                self.assertNotEqual(result["status"], "matching_reported_launch")
                self.assertEqual(result["reason"], reason)
        self.function["payload"]["arguments"] = "malformed private text"
        self.write_rollout()
        self.assertEqual(self.audit()["reason"], "evidence_validation_refused")

    def test_identity_path_and_binding_refuse(self):
        import sqlite3
        for field, value, reason in [("cwd", str(self.base), "thread_cwd_mismatch"),
                                     ("rollout_path", str(self.preparation), "unsafe_source_path")]:
            with self.subTest(field=field):
                with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE threads SET " + field + " = ?", (value,))
                self.assertEqual(self.audit()["reason"], reason)
                with contextlib.closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE threads SET cwd=?, rollout_path=?", (str(self.root), str(self.rollout)))
        self.meta["payload"]["id"] = "foreign-thread"
        self.write_rollout()
        self.assertEqual(self.audit()["reason"], "session_metadata_mismatch")
        self.meta["payload"]["id"] = self.thread
        self.write_rollout()
        retained = self.rollout.with_suffix(".retained")
        self.rollout.rename(retained)
        self.rollout.symlink_to(retained)
        self.assertEqual(self.audit()["reason"], "unsafe_source_path")
        self.rollout.unlink(); retained.rename(self.rollout)
        self.validator.side_effect = ValueError("private exception content")
        result = self.audit()
        self.assertEqual(result["reason"], "current_binding_unverified")
        self.assertNotIn("private exception", json.dumps(result))

    def test_malformed_packet_result_and_wrong_session_cwd_refuse(self):
        self.prepared["native_request"] = {"message": "private incomplete packet"}
        self.write_preparation()
        self.assertEqual(self.audit()["reason"], "preparation_packet_invalid")
        self.prepared["native_request"] = self.packet
        self.write_preparation()
        for response in ("unrecognized private output", json.dumps({"agent_id": "one", "agentId": "two"}),
                         "native_capability_expired native_caller_mismatch"):
            with self.subTest(response_sha256=hashlib.sha256(response.encode()).hexdigest()):
                self.output["payload"]["output"] = response
                self.write_rollout()
                result = self.audit()
                self.assertEqual(result["status"], "unverified")
                self.assertIsNone(result["reported_handle_sha256"])
                self.assertNotIn("private output", json.dumps(result))
        self.meta["payload"]["cwd"] = str(self.base)
        self.write_rollout()
        self.assertEqual(self.audit()["reason"], "session_metadata_mismatch")

    def test_preparation_hash_is_exact_private_file_bytes(self):
        raw = json.dumps(self.prepared, indent=3).encode() + b"\n\n"
        self.preparation.write_bytes(raw)
        self.assertEqual(self.audit()["preparation_sha256"], hashlib.sha256(raw).hexdigest())

    def test_changed_pair_and_fork_cli_refuse(self):
        import io
        for field, value in [("model", "gpt-6.1-sol"), ("reasoning_effort", "max"), ("fork_turns", "all")]:
            with self.subTest(field=field):
                self.function["payload"]["arguments"] = json.dumps(dict(self.packet, **{field:value}))
                self.write_rollout()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = _collector.audit_native_cli(self.root,["--preparation",str(self.preparation),
                        "--thread-id",self.thread,"--call-id",self.call])
                self.assertEqual(code,1)
                self.assertEqual(json.loads(output.getvalue())["mismatched_fields"],[field])

    def test_unrelated_envelopes_do_not_change_exact_selected_hashes(self):
        unrelated_call = {"type":"response_item","payload":{"type":"function_call","call_id":"another_call",
                           "name":"unrelated","arguments":"unparsed invalid JSON private arguments"}}
        unrelated_output = {"type":"response_item","payload":{"type":"function_call_output","call_id":"another_call",
                             "output":"unparsed private output native_capability_expired"}}
        self.write_rollout([self.meta,unrelated_call,self.function,unrelated_output,self.output,unrelated_call])
        result=self.audit()
        self.assertEqual(result["status"],"matching_reported_launch")
        for field, row in [("call_record_sha256",self.function),("result_record_sha256",self.output)]:
            self.assertEqual(result["source"][field],hashlib.sha256(json.dumps(row).encode()+b"\n").hexdigest())
        self.assertEqual(result["reported_handle_sha256"],hashlib.sha256(b"/root/worker").hexdigest())
        self.assertNotIn("unparsed private",json.dumps(result))

    def test_escaped_duplicate_identity_and_oversized_duplicate_refuse(self):
        escaped=json.dumps(self.function).replace(self.call,"\\u0063all_original").encode()+b"\n"
        with self.rollout.open("ab") as stream: stream.write(escaped)
        self.assertEqual(self.audit()["reason"],"call_missing_or_duplicate")
        self.write_rollout()
        duplicate=dict(self.function,padding="x"*1048576)
        with self.rollout.open("ab") as stream: stream.write(json.dumps(duplicate).encode()+b"\n")
        self.assertEqual(self.audit()["reason"],"source_record_invalid")

    def test_duplicate_json_keys_refuse_at_every_decoding_boundary(self):
        original=self.rollout.read_bytes()
        cases=[(self.preparation, b'{"native_request":{},'+self.preparation.read_bytes()[1:]),
               (self.rollout, original.replace(b'"type": "session_meta"',b'"type":"session_meta","type":"session_meta"',1)),
               (self.rollout, original.replace(b'"call_id": "call_original"',b'"call_id":"call_original","call_id":"call_original"',1))]
        for path, raw in cases:
            with self.subTest(path_kind="preparation" if path==self.preparation else "rollout"):
                self.write_preparation();self.rollout.write_bytes(original);path.write_bytes(raw)
                self.assertEqual(self.audit()["reason"],"duplicate_json_key")
        self.write_preparation()
        self.function["payload"]["arguments"]='{"task_name":"one",'+json.dumps(self.packet)[1:]
        self.write_rollout()
        self.assertEqual(self.audit()["reason"],"duplicate_json_key")
        self.function["payload"]["arguments"]=json.dumps(self.packet)
        self.output["payload"]["output"]='{"task_name":"/root/one","task_name":"/root/two"}'
        self.write_rollout()
        self.assertEqual(self.audit()["reason"],"duplicate_json_key")

    def test_concurrent_append_and_same_size_rewrite_refuse(self):
        original=self.rollout.read_bytes()
        real_fstat=os.fstat
        inode=self.rollout.stat().st_ino
        for mutation in ("append","rewrite"):
            self.rollout.write_bytes(original)
            seen=0
            def changing_stat(fd):
                nonlocal seen
                info=real_fstat(fd)
                if info.st_ino==inode:
                    seen+=1
                    if seen==2:
                        if mutation=="append":
                            with self.rollout.open("ab") as stream:stream.write(b"\n")
                        else:
                            changed=original.replace(b"private original task",b"PRIVATE original task")
                            self.assertEqual(len(changed),len(original))
                            self.rollout.write_bytes(changed)
                        info=real_fstat(fd)
                return info
            with self.subTest(mutation=mutation),patch.object(_collector.os,"fstat",side_effect=changing_stat):
                self.assertEqual(self.audit()["reason"],"source_identity_changed")

    def test_partial_tail_completion_during_scan_cannot_cross_snapshot(self):
        partial=self.rollout.read_bytes().rstrip(b"\n")
        self.rollout.write_bytes(partial)
        tail_start=partial.rfind(b"\n")+1
        real_fdopen=os.fdopen
        outer=self
        class AppendOnTail:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):self.stream.__enter__();return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def fileno(self):return self.stream.fileno()
            def readline(self,size):
                if self.stream.tell()==tail_start:
                    outer.assertEqual(size,len(partial)-tail_start)
                    with outer.rollout.open("ab") as stream:stream.write(b"\n")
                return self.stream.readline(size)
        def fdopen(fd,*args,**kwargs):
            stream=real_fdopen(fd,*args,**kwargs)
            return AppendOnTail(stream) if os.fstat(fd).st_ino==self.rollout.stat().st_ino else stream
        with patch.object(_collector.os,"fdopen",side_effect=fdopen):
            result=self.audit()
        self.assertEqual(result["status"],"unverified")
        self.assertEqual(result["reason"],"source_record_invalid")

    def test_supported_nickname_is_not_a_refusal_and_mismatch_survives_unknowns(self):
        self.output["payload"]["output"]=json.dumps({"task_name":"/root/worker","nickname":"native_capability_expired"})
        self.write_rollout()
        self.assertEqual(self.audit()["status"],"matching_reported_launch")
        self.function["payload"]["arguments"]=json.dumps(dict(self.packet,task_name="changed_task"))
        self.output["payload"]["output"]="unknown private output"
        self.write_rollout()
        result=self.audit()
        self.assertEqual(result["status"],"argument_mismatch")
        self.assertEqual(result["native_result"],"unverified")
        self.function["timestamp"]="malformed"
        self.write_rollout()
        result=self.audit()
        self.assertEqual(result["status"],"argument_mismatch")
        self.assertEqual(result["mismatched_fields"],["task_name"])
        self.assertIsNone(result["fresh_at_call"])

    def opaque_records(self):
        import base64
        self.actual = dict(self.packet, message=base64.urlsafe_b64encode(b"\x80" + b"\0" * 8 + b"x" * 64).decode())
        self.function["payload"]["arguments"] = json.dumps(self.actual)
        self.prepared.update(preparation_id=self.preparation.stem, task_id="task:opaque", caller_id=self.thread,
            capability_provenance="caller_supplied_tool_metadata", receipt={"capability_tool": "collaboration.spawn_agent"},
            project_binding={"root": str(self.root)}, spawn_agent={k:self.packet[k] for k in ("model", "reasoning_effort", "fork_turns")},
            native_dispatch={"task_name":"declared_task", "message_sha256":hashlib.sha256(self.packet["message"].encode()).hexdigest()},
            native_transport={"schema":"CPI414NATIVETRANSPORTV1", "task_name":self.packet["task_name"]})
        self.write_preparation()
        arg_hash = hashlib.sha256(json.dumps(self.actual, sort_keys=True).encode()).hexdigest()
        msg_hash = hashlib.sha256(self.actual["message"].encode()).hexdigest()
        identity={"session_id":self.thread, "tool_use_id":self.call, "turn_id":"turn:actual"}
        self.consume = {"schema":"CPI414NATIVECONSUMPTIONV2", "status":"pending_native_result", "identity":identity,
            "preparation_id":self.prepared["preparation_id"], "task_id":self.prepared["task_id"],
            "project_binding":self.prepared["project_binding"], "prepared_caller_id":self.thread,
            "caller_mapping":"exact_session_uuid", "native_transport":self.prepared["native_transport"],
            "declared_task":self.prepared["native_dispatch"], "declared_message_sha256":self.prepared["native_dispatch"]["message_sha256"],
            "prepared_pair":self.prepared["spawn_agent"], "requested_pair":self.prepared["spawn_agent"],
            "plaintext_attestation":"unavailable", "observed_message_sha256":msg_hash,
            "observed_pre_arguments_sha256":arg_hash, "applied_arguments_sha256":arg_hash}
        self.post = {"schema":"CPI414NATIVERESULTV2", "status":"native_handle_observed", "identity":identity,
            "preparation_id":self.prepared["preparation_id"], "declared_task":self.prepared["native_dispatch"],
            "plaintext_attestation":"unavailable", "observed_post_message_sha256":msg_hash,
            "observed_post_arguments_sha256":arg_hash, "native_handle":"/root/worker"}
        self.write_hook_records()
        self.write_rollout()

    def write_hook_records(self):
        for suffix, data in [(".consume.json", self.consume), (".result.json", self.post)]:
            path = self.preparation.with_suffix(suffix)
            path.write_text(json.dumps(data, sort_keys=True) + "\n")
            path.chmod(0o600)

    def test_opaque_original_requires_exact_existing_hook_binding_without_plaintext_claim(self):
        self.opaque_records()
        paths=[self.preparation, self.preparation.with_suffix(".consume.json"),self.preparation.with_suffix(".result.json")]
        before={p:p.read_bytes() for p in paths}
        result=self.audit()
        self.assertEqual(result["status"], "matching_reported_launch")
        self.assertTrue(result["observable_arguments_match"])
        self.assertIsNone(result["arguments_match"])
        self.assertEqual(result["plaintext_attestation"], "unavailable")
        self.assertEqual(result["caller_acceptance"], "not_performed")
        self.assertNotEqual(result["expected_message_sha256"], result["actual_message_sha256"])
        self.assertEqual(result["hook_binding"]["canonical_arguments_sha256"], result["actual_hook_arguments_sha256"])
        self.assertNotEqual(result["actual_arguments_sha256"], result["actual_hook_arguments_sha256"])
        self.assertEqual(result["hook_binding"]["consume_sha256"], hashlib.sha256(before[paths[1]]).hexdigest())
        self.assertEqual(before,{p:p.read_bytes() for p in paths})
        self.assertNotIn(self.actual["message"],json.dumps(result))

    def test_opaque_incident_and_expired_refusal_need_no_fabricated_hook_records(self):
        self.opaque_records()
        self.preparation.with_suffix(".consume.json").unlink()
        self.preparation.with_suffix(".result.json").unlink()
        self.actual["task_name"]="changed_task"
        self.function["payload"]["arguments"]=json.dumps(self.actual)
        self.output["payload"]["output"]="native_plaintext_marker_required"
        self.write_rollout()
        result=self.audit()
        self.assertEqual(result["status"],"argument_mismatch")
        self.assertEqual(result["mismatched_fields"],["task_name"])
        self.assertEqual(result["plaintext_attestation"],"unavailable")
        self.actual["task_name"]=self.packet["task_name"]
        self.function["payload"]["arguments"]=json.dumps(self.actual)
        self.output["payload"]["output"]="native_capability_expired"
        self.write_rollout()
        result=self.audit()
        self.assertEqual(result["status"],"matching_without_launch")
        self.assertEqual(result["refusal_reason"],"native_capability_expired")
        self.assertIsNone(result["arguments_match"])

    def test_opaque_missing_tampered_and_foreign_hook_bindings_refuse(self):
        self.opaque_records()
        self.preparation.with_suffix(".result.json").unlink()
        self.assertEqual(self.audit()["reason"], "hook_evidence_missing")
        for section, field, value in [("consume","project_binding",{"root":"foreign"}),
                ("consume","task_id","foreign"),("consume","preparation_id","foreign"),
                ("consume","native_transport",{}),("consume","observed_message_sha256","wrong"),
                ("consume","applied_arguments_sha256","wrong"),
                ("post","observed_post_arguments_sha256","wrong"),("post","native_handle","/root/foreign"),
                ("post","identity",{"session_id":self.thread,"tool_use_id":"foreign","turn_id":"turn:actual"})]:
            with self.subTest(section=section,field=field):
                self.opaque_records()
                record=self.consume if section=="consume" else self.post
                record[field]=value
                self.write_hook_records()
                result=self.audit()
                self.assertEqual(result["status"],"unverified")
                self.assertIsNone(result["hook_binding"])

    def test_cli_reports_only_matching_launch_as_success(self):
        import contextlib
        import io
        for refusal, status in [(False, 0), (True, 1)]:
            if refusal:
                self.output["payload"]["output"] = "native_capability_expired"
                self.write_rollout()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = _collector.audit_native_cli(self.root, ["--preparation", str(self.preparation),
                      "--thread-id", self.thread, "--call-id", self.call])
            self.assertEqual(code, status)
            self.assertEqual(json.loads(output.getvalue())["arguments_match"], True)


class UsageCollectorTests(unittest.TestCase):
    def test_long_context_pricing_is_per_response_and_preserves_unknowns(self):
        short = {"input_uncached": 2, "input_cached": .1, "cache_write": 2.5, "output": 10}
        long = {"input_uncached": 4, "input_cached": .2, "cache_write": 5, "output": 15}
        exact = api_price_proxy({"input_tokens": 272_000, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 1}, short,
                                long_rates=long)
        above = api_price_proxy({"input_tokens": 272_001, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 1}, short,
                                long_rates=long)
        self.assertEqual(exact["context_band"], "short")
        self.assertEqual(above["context_band"], "long")
        self.assertAlmostEqual(exact["estimate"], (272_000 * 2 + 10) / 1_000_000)
        self.assertAlmostEqual(above["estimate"], (272_001 * 4 + 15) / 1_000_000)

        # A turn can contain both bands; apply the threshold to each row, not the sum.
        baseline = {"pricing": {"rates": {"gpt-6.1-sol": {**short, "long_context": long}}}}
        rows = [{"type": "usage", "source_id": THREAD, "turn_id": "mixed", "model": "gpt-6.1-sol",
                 "effort": "medium", "tokens": {"input_tokens": 100, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 10, "output_tokens": 4}},
                {"type": "usage", "source_id": THREAD, "turn_id": "mixed", "model": "gpt-6.1-sol",
                 "effort": "medium", "tokens": {"input_tokens": 272_001, "cached_input_tokens": 10,
                 "cache_write_input_tokens": 5, "output_tokens": 2}}]
        group = estimate_groups(rows, baseline)[0]
        expected = ((70 * 2 + 20 * .1 + 10 * 2.5 + 4 * 10) +
                    (272_001 - 10 - 5) * 4 + 10 * .2 + 5 * 5 + 2 * 15) / 1_000_000
        self.assertAlmostEqual(group["price_proxy_usd"], expected)
        self.assertTrue(group["price_complete"])
        self.assertIn("long, short", group["price_basis"])
        self.assertIn("conditional Standard", group["cost_basis"])

        aggregate_only = estimate_groups([{"type": "reported_snapshot", "source_id": THREAD,
            "turn_id": "aggregate", "model": "gpt-6.1-sol", "effort": "medium",
            "tokens": {"input_tokens": 675_660, "cached_input_tokens": 602_624,
                       "cache_write_input_tokens": 0, "output_tokens": 13_276}}], baseline)[0]
        self.assertIsNone(aggregate_only["price_proxy_usd"])
        self.assertIn("per-request context bands unknown", aggregate_only["price_basis"])

    def test_long_context_cache_write_missing_rates_and_model_remain_explicit(self):
        short = {"input_uncached": 2, "input_cached": .1, "cache_write": 2.5, "output": 10}
        long = {"input_uncached": 4, "input_cached": .2, "cache_write": 5, "output": 15}
        no_write_count = api_price_proxy({"input_tokens": 272_001, "cached_input_tokens": 1,
                                          "output_tokens": 2}, short, long_rates=long)
        self.assertLess(no_write_count["low"], no_write_count["high"])
        self.assertIn("cache-write", no_write_count["basis"])
        missing_long = api_price_proxy({"input_tokens": 272_001, "cached_input_tokens": 0,
                                        "cache_write_input_tokens": 0, "output_tokens": 2}, short)
        self.assertIsNone(missing_long["estimate"])
        self.assertEqual(missing_long["basis"], "long-context published rates unavailable")
        unknown_model = estimate_groups([{"type": "usage", "source_id": THREAD, "turn_id": "u",
            "model": None, "effort": None, "tokens": {"input_tokens": 272_001, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 2}}], {"pricing": {"rates": {}}})[0]
        self.assertIsNone(unknown_model["price_proxy_usd"])
        self.assertFalse(unknown_model["price_complete"])

    def test_future_only_registration_ignores_history_and_collects_new_deltas(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); sessions = root / "sessions"; archived = root / "archived"
            sessions.mkdir(); archived.mkdir()
            stream = sessions / f"rollout-test-{THREAD}.jsonl"; ledger = root / "usage.jsonl"
            line(stream, session_meta())
            line(stream, context("old-turn", "gpt-6.1-sol", "high"))
            line(stream, usage("old-turn", "old-response", 900000))
            manifest = root / "source.json"; manifest.write_text(json.dumps(future_manifest(stream)))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                self.assertEqual(collect_future_source_file(manifest, ledger), 0)
                curs = [r for r in read_records(ledger) if r.get("type") == "future_cursor"]
                self.assertEqual(curs[-1]["offset"], stream.stat().st_size)
                self.assertEqual(curs[-1]["contexts"], {})
                self.assertEqual([r for r in read_records(ledger) if r.get("type") == "usage"], [])
                # A newly appended old/missing-time context cannot supply model or effort.
                line(stream, {**context("old-turn", "gpt-6.1-sol", "high"), "timestamp": "2026-10-19T00:00:00Z"})
                oldctx = usage("old-turn", "new-response-old-context", 3)
                oldctx["timestamp"] = fresh_timestamp(30)
                line(stream, oldctx)
                stale_usage = usage("stale-turn", "stale-after-baseline", 999999)
                stale_usage["timestamp"] = "2026-10-19T00:00:00Z"
                line(stream, stale_usage)
                missing_time_usage = usage("missing-time-turn", "missing-time-after-baseline", 999999)
                missing_time_usage.pop("timestamp")
                line(stream, missing_time_usage)
                line(stream, context("missing-time", "gpt-6.1-sol", "high"))
                no_time_ctx = usage("missing-time", "new-response-missing-context-time", 2)
                no_time_ctx["timestamp"] = fresh_timestamp(45)
                line(stream, no_time_ctx)
                line(stream, {**context("new-turn", "gpt-6-luna", "medium"), "timestamp": fresh_timestamp()})
                fresh = usage("new-turn", "new-response", 8)
                fresh["timestamp"] = fresh_timestamp(120)
                line(stream, fresh)
                orphan = usage("old-turn", "new-response-no-context", 4)
                orphan["timestamp"] = fresh_timestamp(180)
                line(stream, orphan)
                self.assertEqual(collect_future_source_file(manifest, ledger), 4)
                self.assertEqual(collect_future_source_file(manifest, ledger), 0)
            rows = [r for r in read_records(ledger) if r.get("type") == "usage"]
            self.assertEqual([r["response_id"] for r in rows], ["new-response-old-context", "new-response-missing-context-time", "new-response", "new-response-no-context"])
            self.assertIsNone(rows[0]["model"]); self.assertIsNone(rows[0]["effort"])
            self.assertIsNone(rows[1]["model"]); self.assertIsNone(rows[1]["effort"])
            self.assertEqual((rows[2]["model"], rows[2]["effort"], rows[2]["tokens"]["input_tokens"]),
                             ("gpt-6-luna", "medium", 8))
            self.assertIsNone(rows[3]["model"]); self.assertIsNone(rows[3]["effort"])
            future_cursor=[r for r in read_records(ledger) if r.get("type")=="future_cursor"][-1]
            self.assertGreaterEqual(future_cursor["unverified_timestamp_events"],4)
            self.assertNotIn("stale-after-baseline", {r.get("response_id") for r in rows})
            self.assertNotIn("missing-time-after-baseline", {r.get("response_id") for r in rows})
            self.assertFalse(any(r.get("type") == "reported_snapshot" for r in read_records(ledger)))

    def test_future_only_initial_partial_tail_is_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sessions=root/"sessions"; archived=root/"archived"
            sessions.mkdir(parents=True); archived.mkdir(parents=True); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream, session_meta())
            stale=usage("old-turn", "old-tail", 500)
            tail=json.dumps(stale).encode()
            with stream.open("ab") as f: f.write(tail[:40])
            manifest=root/"source.json"; manifest.write_text(json.dumps(future_manifest(stream)))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                self.assertEqual(collect_future_source_file(manifest, ledger), 0)
                cursor=[r for r in read_records(ledger) if r.get("type")=="future_cursor"][-1]
                self.assertTrue(cursor["discard_partial_until_newline"])
                with stream.open("ab") as f: f.write(tail[40:]+b"\n")
                line(stream, {**context("new-turn"), "timestamp": fresh_timestamp()})
                good=usage("new-turn","future-only",9); good["timestamp"]=fresh_timestamp(120); line(stream,good)
                self.assertEqual(collect_future_source_file(manifest, ledger),1)
            rows=[r for r in read_records(ledger) if r.get("type")=="usage"]
            self.assertEqual([r["response_id"] for r in rows],["future-only"])

    def test_future_only_refuses_identity_change_truncation_and_bad_session(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sessions=root/"sessions"; archived=root/"archived"
            sessions.mkdir(parents=True); archived.mkdir(parents=True); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream,session_meta())
            manifest=root/"source.json"; manifest.write_text(json.dumps(future_manifest(stream)))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                self.assertEqual(collect_future_source_file(manifest,ledger),0)
                stream.write_bytes(b"")
                with self.assertRaisesRegex(ValueError,"truncated|identity changed"):
                    collect_future_source_file(manifest,ledger)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sessions=root/"sessions"; archived=root/"archived"
            sessions.mkdir(); archived.mkdir(); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream,session_meta())
            manifest_obj=future_manifest(stream); manifest_obj["session_id"]="fedcba9876543210"
            manifest=root/"source.json"; manifest.write_text(json.dumps(manifest_obj))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                with self.assertRaisesRegex(ValueError,"session mismatch"):
                    collect_future_source_file(manifest,ledger)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sessions=root/"sessions"; archived=root/"archived"
            sessions.mkdir(); archived.mkdir(); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream,session_meta())
            manifest=root/"source.json"; manifest.write_text(json.dumps(future_manifest(stream)))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                self.assertEqual(collect_future_source_file(manifest,ledger),0)
                old=stream.with_suffix(".old"); stream.rename(old); line(stream,session_meta())
                with self.assertRaisesRegex(ValueError,"identity changed"):
                    collect_future_source_file(manifest,ledger)

    def test_future_only_requires_fresh_timestamp_and_matching_usage_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sessions=root/"sessions"; archived=root/"archived"
            sessions.mkdir(); archived.mkdir(); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream,session_meta())
            manifest=root/"source.json"; manifest.write_text(json.dumps(future_manifest(stream)))
            with patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                self.assertEqual(collect_future_source_file(manifest,ledger),0)
                # Context with a missing timestamp cannot bleed into the following new response.
                line(stream,context("old-turn","gpt-6.1-sol","high"))
                bad=usage("new-turn","wrong-session",10,session="fedcba9876543210")
                bad["timestamp"]=fresh_timestamp(120); line(stream,bad)
                with self.assertRaisesRegex(ValueError,"thread/session mismatch"):
                    collect_future_source_file(manifest,ledger)

    def test_future_only_cannot_be_downgraded_through_source_or_event_entrypoints(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "work/factory-tests/collector") as d:
            root=Path(d); home=root/"home"; sessions=home/".codex/sessions"; archived=home/".codex/archived_sessions"
            sessions.mkdir(parents=True); archived.mkdir(parents=True); stream=sessions/f"rollout-test-{THREAD}.jsonl"; ledger=root/"usage.jsonl"
            line(stream,session_meta()); line(stream,context("history","gpt-6.1-sol","high"))
            line(stream,usage("history","historical-response",999999))
            manifest_obj=future_manifest(stream); manifest=root/"future.json"
            manifest.write_text(json.dumps(manifest_obj))
            with patch("factory.usage_collector.Path.home", return_value=home), \
                 patch("factory.usage_collector.SESSIONS", sessions), patch("factory.usage_collector.ARCHIVED", archived):
                # An initial future manifest passed through --source remains future-only.
                self.assertEqual(collect_source_file(manifest,ledger),0)
                self.assertEqual(collect_source_file(manifest,ledger),0)
                downgraded=dict(manifest_obj); downgraded.pop("collection_mode")
                normal_manifest=root/"historical.json"; normal_manifest.write_text(json.dumps(downgraded))
                with self.assertRaisesRegex(ValueError,"cannot be downgraded"):
                    collect_source_file(normal_manifest,ledger)
                with self.assertRaisesRegex(ValueError,"cannot be downgraded"):
                    collect_rollout({k:v for k,v in manifest_obj.items() if k!="collection_mode"},ledger)

                # A verified ordinary result receipt for this registered thread also dispatches to future-only.
                native=root/"native.json"; native.write_text(json.dumps({"thread_id":THREAD}))
                receipt=root/"receipt.json"
                receipt.write_text(json.dumps({"dispatch_id":"future-dispatch","thread_id":THREAD,"acceptance":True,
                    "native_evidence":{"path":str(native),"sha256":hashlib.sha256(native.read_bytes()).hexdigest()}}))
                fresh_context=context("capacity-binding","gpt-6.1-sol","high")
                fresh_context["timestamp"]=fresh_timestamp(120)
                line(stream,fresh_context)
                fresh_usage=usage("capacity-binding","capacity-response",17)
                fresh_usage["timestamp"]=fresh_timestamp(130)
                line(stream,fresh_usage)
                event=root/"event.json"
                event.write_text(json.dumps({"task_id":"different-result-task","dispatch_id":"future-dispatch","receipt":{
                    "path":str(receipt.relative_to(ROOT)),"sha256":hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
                self.assertEqual(collect_event(event,ledger),1)
                self.assertEqual(collect_event(event,ledger),0)
                # A task result must not attach bounded acceptance to the long-lived future source.
                self.assertEqual(collect_future_source_file(manifest,ledger),0)

            rows=read_records(ledger)
            usages=[r for r in rows if r.get("type")=="usage"]
            self.assertEqual(len(usages),1)
            self.assertEqual(usages[0]["response_id"],"capacity-response")
            self.assertIsNone(usages[0]["task_id"])
            self.assertEqual(usages[0]["task_class"],"coordinator-overhead")
            self.assertEqual(usages[0]["model"],"gpt-6.1-sol")
            self.assertEqual(usages[0]["effort"],"high")
            self.assertEqual([r for r in rows if r.get("type")=="cursor"],[])
            sources=[r for r in rows if r.get("type")=="source"]
            self.assertIsNone(sources[-1].get("outcome"))
            self.assertEqual(len([r for r in rows if r.get("type")=="future_cursor"]),5) # baseline and safe source/event readbacks

    def test_incremental_resume_dedup_partial_line_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stream, ledger = root / "rollout.jsonl", root / "usage.jsonl"
            line(stream, {"type": "event_msg", "payload": {"text": "private prompt is ignored"}})
            line(stream, session_meta())
            line(stream, context())
            line(stream, usage("turn-a", "resp-1", 5, session=PARENT_SESSION))
            source = {"type": "source", "source_id": THREAD, "task_id": "task-a",
                      "thread_id": THREAD, "session_id": PARENT_SESSION, "path": str(stream), "task_class": None,
                      "difficulty_signals": None, "route": "unknown", "outcome": None}
            self.assertEqual(collect_rollout(source, ledger), 1)
            self.assertEqual(collect_rollout(source, ledger), 0)
            with ledger.open("ab") as f: f.write(b'{"type":"usage"}')
            line(stream, usage("turn-a", "resp-1", 5, session=PARENT_SESSION))
            line(stream, usage("turn-a", "resp-2", 7, session=PARENT_SESSION))
            pending = json.dumps(usage("turn-a", "resp-1", 5, session=PARENT_SESSION)).encode()
            with stream.open("ab") as f:
                f.write(pending[:37])
            self.assertEqual(collect_rollout(source, ledger), 1)
            cursor = [r for r in read_records(ledger) if r.get("type") == "cursor"][-1]
            partial_start = stream.stat().st_size - 37
            self.assertEqual(cursor["offset"], partial_start)
            with stream.open("ab") as f:
                f.write(pending[37:] + b"\n")
            self.assertEqual(collect_rollout(source, ledger), 0)
            # A truncated/replayed stream starts at zero; response identity still prevents recounting.
            stream.write_bytes(b"")
            line(stream, session_meta())
            line(stream, context())
            line(stream, usage("turn-a", "resp-2", 7, session=PARENT_SESSION))
            self.assertEqual(collect_rollout(source, ledger), 0)
            rows = [r for r in read_records(ledger) if r.get("type") == "usage"]
            self.assertEqual([r["response_id"] for r in rows], ["resp-1", "resp-2"])
            self.assertEqual(sum(r["tokens"]["input_tokens"] for r in rows), 12)
            self.assertTrue(all("private prompt" not in json.dumps(r) for r in read_records(ledger)))
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in ledger.read_text().splitlines()))

    def test_rejects_cross_session_usage(self):
        with tempfile.TemporaryDirectory() as d:
            stream, ledger = Path(d) / "rollout.jsonl", Path(d) / "usage.jsonl"
            line(stream, session_meta())
            line(stream, context())
            line(stream, usage("turn-a", "resp-1", 1, "fedcba9876543210"))
            with self.assertRaisesRegex(ValueError, "thread/session mismatch"):
                collect_rollout({"source_id": THREAD, "thread_id": THREAD, "path": str(stream)}, ledger)
            stream.write_bytes(b"")
            line(stream, session_meta())
            line(stream, context())
            line(stream, usage("turn-a", "resp-2", 1, session="fedcba9876543210"))
            with self.assertRaisesRegex(ValueError, "thread/session mismatch"):
                collect_rollout({"source_id": THREAD, "thread_id": THREAD, "session_id": THREAD,
                                 "path": str(stream)}, ledger)
            stream.write_bytes(b"")
            line(stream, session_meta("fedcba9876543210"))
            line(stream, context())
            line(stream, usage("turn-a", "resp-3", 1))
            with self.assertRaisesRegex(ValueError, "session metadata ID does not match"):
                collect_rollout({"source_id": THREAD, "thread_id": THREAD, "path": str(stream)}, ledger)

    def test_parseable_unterminated_record_waits_for_newline_and_cost_range(self):
        from factory.usage_collector import api_price_proxy
        with tempfile.TemporaryDirectory() as d:
            stream, ledger = Path(d) / "rollout.jsonl", Path(d) / "usage.jsonl"
            line(stream, session_meta())
            line(stream, context())
            start = stream.stat().st_size
            with stream.open("ab") as f:
                f.write(json.dumps(usage("turn-a", "resp-1", 10)).encode())
            source = {"source_id": THREAD, "thread_id": THREAD, "path": str(stream)}
            self.assertEqual(collect_rollout(source, ledger), 0)
            cursor = [r for r in read_records(ledger) if r.get("type") == "cursor"][-1]
            self.assertEqual(cursor["offset"], start)
            with stream.open("ab") as f: f.write(b"\n")
            self.assertEqual(collect_rollout(source, ledger), 1)
        estimate = api_price_proxy({"input_tokens": 100, "cached_input_tokens": 20,
                                    "output_tokens": 4}, {"input_uncached": 2, "input_cached": .1,
                                                          "cache_write": 2.5, "output": 10})
        self.assertEqual(estimate["estimate"], .000202)
        self.assertEqual(estimate["low"], .000202)
        self.assertEqual(estimate["high"], .000242)

    def test_estimator_preserves_unknown_workflow_outcome(self):
        baseline = {"pricing": {"rates": {"gpt-6-luna": {"input_uncached": .1, "input_cached": .01,
                    "cache_write": .125, "output": .5}}}}
        rows = [{"type": "source", "source_id": THREAD, "outcome": None},
                {"type": "usage", "source_id": THREAD, "turn_id": "turn", "model": "gpt-6-luna",
                 "effort": "medium", "tokens": {"input_tokens": 10, "cached_input_tokens": 0,
                 "cache_write_input_tokens": 0, "output_tokens": 1}}]
        group = estimate_groups(rows, baseline)[0]
        self.assertIsNone(group["workflow_accepted"])
        self.assertIsNone(group["artifact_accepted"])
        self.assertIsNone(group["accepted_task_cost_proxy_usd"])
        self.assertEqual(expected_cost_per_accepted(12.0, .5), 24.0)
        self.assertIsNone(expected_cost_per_accepted(12.0, None))
        rows.append({"type": "reported_snapshot", "source_id": THREAD, "turn_id": "turn",
                     "model": "gpt-6-luna", "effort": "medium",
                     "tokens": {"input_tokens": 999999, "cached_input_tokens": 0,
                                "cache_write_input_tokens": 0, "output_tokens": 0}})
        self.assertEqual(estimate_groups(rows, baseline)[0]["response_count"], 1)
        accepted_source = {"type": "source", "source_id": THREAD, "outcome": {"whole_workflow_accepted": True}}
        two_turns = [accepted_source,
                     {"type": "usage", "source_id": THREAD, "turn_id": "turn-a", "model": "gpt-6-luna",
                      "effort": "medium", "tokens": {"input_tokens": 10, "cached_input_tokens": 0,
                      "cache_write_input_tokens": 0, "output_tokens": 1}},
                     {"type": "usage", "source_id": THREAD, "turn_id": "turn-b", "model": "gpt-6-luna",
                      "effort": "medium", "tokens": {"input_tokens": 10, "cached_input_tokens": 0,
                      "cache_write_input_tokens": 0, "output_tokens": 1}}]
        turns = estimate_groups(two_turns, baseline)
        self.assertTrue(all(t["workflow_accepted"] is True for t in turns))
        self.assertTrue(all(t["accepted_task_cost_proxy_usd"] is None for t in turns))
        self.assertTrue(all(t["observed_task_turn_count"] == 2 for t in turns))
        self.assertEqual(turns[0]["observed_task_cost_proxy_usd"], turns[1]["observed_task_cost_proxy_usd"])
        incomplete = estimate_groups([accepted_source, two_turns[1],
            {"type": "usage", "source_id": THREAD, "turn_id": "turn-a", "model": "gpt-6-luna",
             "effort": "medium", "tokens": {"input_tokens": 10, "cached_input_tokens": 0,
             "cache_write_input_tokens": 0, "output_tokens": None}}], baseline)[0]
        self.assertIsNone(incomplete["price_proxy_usd"])
        self.assertIsNone(incomplete["price_low_usd"])
        self.assertIsNone(incomplete["price_high_usd"])

    def test_shared_quota_intervals_remain_unallocated_and_reset_aware(self):
        a = {"captured_at": "2026-10-19T17:00:00Z", "used_percent": 42,
             "window_id": "weekly-A", "limit_id": "codex", "window_duration_mins": 10080,
             "reset_timestamp_unix": 1792992743}
        b = {"captured_at": "2026-10-19T17:54:15Z", "used_percent": 43,
             "window_id": "weekly-A", "limit_id": "codex", "window_duration_mins": 10080,
             "reset_timestamp_unix": 1792992743}
        delta = quota_interval(a, b)
        self.assertEqual(delta["used_percent_delta"], 1)
        self.assertEqual(delta["status"], "same_window_shared_delta_unallocated")
        self.assertIsNone(delta["attributed_to_model_or_task"])
        changed = quota_interval(a, {**b, "reset_timestamp_unix": 1792999999})
        self.assertIsNone(changed["used_percent_delta"])
        self.assertEqual(changed["status"], "window_or_reset_changed")
        missing = quota_interval({**a, "reset_timestamp_unix": None}, b)
        self.assertIsNone(missing["used_percent_delta"])
        self.assertEqual(missing["status"], "unknown_missing_window_or_reset_id")
        decreased = quota_interval(b, {**b, "captured_at": "2026-10-19T18:00:00Z", "used_percent": 41})
        self.assertIsNone(decreased["used_percent_delta"])
        self.assertEqual(decreased["status"], "counter_decreased_or_reset")
        wrong_limit = quota_interval(a, {**b, "limit_id": "other"})
        self.assertEqual(wrong_limit["status"], "window_or_reset_changed")
        wrong_duration = quota_interval(a, {**b, "window_duration_mins": 60})
        self.assertEqual(wrong_duration["status"], "window_or_reset_changed")
        reversed_time = quota_interval(b, a)
        self.assertEqual(reversed_time["status"], "unknown_nonchronological_timestamp")
        out_of_range = quota_interval(a, {**b, "used_percent": 101})
        self.assertEqual(out_of_range["status"], "unknown_counter_out_of_bounds")

    def test_bounded_acceptance_is_separate_from_factory_and_product_status(self):
        baseline = {"pricing": {"rates": {"gpt-6.1-sol": {"input_uncached": 2, "input_cached": .1,
                    "cache_write": 2.5, "output": 10}}}}
        rows = [{"type": "source", "source_id": THREAD, "outcome": {
                    "bounded_deliverable_accepted": True, "native_workflow_succeeded": True,
                    "whole_workflow_accepted": True, "product_integration_accepted": False}},
                {"type": "usage", "source_id": THREAD, "turn_id": "turn", "model": "gpt-6.1-sol",
                 "effort": "medium", "tokens": {"input_tokens": 100, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 0, "output_tokens": 4}}]
        group = estimate_groups(rows, baseline)[0]
        self.assertTrue(group["bounded_deliverable_accepted"])
        self.assertTrue(group["workflow_accepted"])
        self.assertTrue(group["native_workflow_succeeded"])
        self.assertFalse(group["product_integration_accepted"])
        self.assertIsNotNone(group["observed_accepted_worker_turn_cost_proxy_usd"])
        self.assertIsNone(group["accepted_task_cost_proxy_usd"])

        failed_workflow = dict(rows[0], outcome={"bounded_deliverable_accepted": True,
                                                 "whole_workflow_accepted": False,
                                                 "factory_workflow_succeeded": False})
        luna_group = estimate_groups([failed_workflow, rows[1]], baseline)[0]
        self.assertFalse(luna_group["workflow_accepted"])
        self.assertTrue(luna_group["bounded_deliverable_accepted"])
        self.assertIsNotNone(luna_group["observed_accepted_worker_turn_cost_proxy_usd"])

    @unittest.skipUnless((ROOT / "scripts/project-coordinator").is_file(), "Native Core wrapper belongs to its project; shared runtime does not install a coordinator")
    def test_verified_result_event_and_wrapper_hook_preserve_output_and_status(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "work/factory-tests/collector") as d:
            tmp = Path(d)
            stream, ledger = tmp / "rollout.jsonl", tmp / "usage.jsonl"
            line(stream, session_meta())
            line(stream, context())
            line(stream, usage("turn-a", "resp-1", 9))
            task_id = "mock-owned-result"
            source_id = THREAD
            line(ledger, {"type": "source", "source_id": source_id, "task_id": task_id,
                          "thread_id": THREAD, "path": str(stream), "task_class": "fixture-real-seam",
                          "difficulty_signals": None, "route": "unknown", "outcome": None})
            native = tmp / "native.json"
            native.write_text(json.dumps({"thread_id": THREAD}))
            receipt = tmp / "result.json"
            receipt.write_text(json.dumps({"dispatch_id": "dispatch-a", "thread_id": THREAD, "acceptance": True, "native_evidence": {
                "path": str(native), "sha256": hashlib.sha256(native.read_bytes()).hexdigest()}}))
            event = tmp / "event.json"
            event.write_text(json.dumps({"task_id": task_id, "dispatch_id": "dispatch-a", "receipt": {
                "path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
            fakebin = tmp / "bin"
            fakebin.mkdir()
            uv = fakebin / "uv"
            uv.write_text("#!/bin/sh\nprintf '{\\\"mock\\\":true}\\n'\nexit \"${MOCK_UV_EXIT:-0}\"\n")
            uv.chmod(0o755)
            wrapper = ROOT / "scripts/project-coordinator"
            env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}", CPI404_USAGE_LEDGER=str(ledger),
                       MOCK_UV_EXIT="0")
            ok = subprocess.run([str(wrapper), "--db", "unused", "result", "--input", str(event)],
                                cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual((ok.returncode, ok.stdout), (0, '{"mock":true}\n'))
            rows = read_records(ledger)
            self.assertEqual([r["response_id"] for r in rows if r.get("type") == "usage"], ["resp-1"])
            latest_source = [r for r in rows if r.get("type") == "source"][-1]
            self.assertTrue(latest_source["outcome"]["bounded_deliverable_accepted"])
            self.assertIsNone(latest_source["outcome"].get("whole_workflow_accepted"))
            native.write_text(json.dumps({"thread_id": "fedcba9876543210"}))
            receipt.write_text(json.dumps({"dispatch_id": "dispatch-a", "thread_id": THREAD, "acceptance": True,
                "native_evidence": {"path": str(native), "sha256": hashlib.sha256(native.read_bytes()).hexdigest()}}))
            event.write_text(json.dumps({"task_id": task_id, "dispatch_id": "dispatch-a", "receipt": {
                "path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
            with self.assertRaisesRegex(ValueError, "receipt/native thread ID mismatch"):
                collect_event(event, ledger)
            env["MOCK_UV_EXIT"] = "7"
            failed = subprocess.run([str(wrapper), "--db", "unused", "result", "--input", str(event)],
                                    cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(failed.returncode, 7)
            self.assertEqual(len([r for r in read_records(ledger) if r.get("type") == "usage"]), 1)


if __name__ == "__main__":
    unittest.main()
