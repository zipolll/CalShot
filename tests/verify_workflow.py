"""Structural and fixture checks only; this does not emulate iOS permissions/UI."""
import copy
import base64
import datetime as dt
import hashlib
import json
import plistlib
import re
from calshot import __version__

from calshot.shortcut import (CALENDAR_CARRIER_NAME, CALENDAR_SELECTION_CHECK_NAME, CALENDAR_SELECTION_CHECK_STEM, DEFAULT_CALENDAR_SLOTS, ENDPOINT, FILE_STEM, MODEL, NAME,
                             PLACEHOLDER, REMINDER_MODES, ROOT, SYSTEM_PROMPT, api_key_question,
                             build, build_editor_check, build_reminder_check, build_reminder_runtime_check, build_calendar_selection_check, build_performance_check,
                             write, write_reminder_check)

# Sample device selections, not fixed production categories or discovered IDs.
SAMPLE_CALENDARS = ("生活", "学习", "作业", "探索", "科研")


def refs(value):
    if isinstance(value, dict):
        if value.get("Type") == "ActionOutput":
            yield value["OutputUUID"]
        for v in value.values():
            yield from refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)


def structure(workflow, require_key=True, runtime_reminder=False, calendar_selection_probe=False, calendar_import_probe=False):
    seen, stack = set(), []
    producers = {}
    for a in workflow["WFWorkflowActions"]:
        p = a["WFWorkflowActionParameters"]
        assert p.get("WFAlertActionTitle") != "请核对这条日程", "A static review alert still blocks the editable calendar sheet"
        assert p["UUID"] not in seen, "Duplicate UUID"
        assert set(refs(p)) <= seen, "Forward or dangling output reference"
        for value in walk(p):
            if isinstance(value, dict) and value.get("Type") == "ActionOutput":
                producer = producers[value["OutputUUID"]]
                assert value["OutputName"] and value["OutputName"] == producer.get("CustomOutputName"), "Output reference name must match its producer"
            if isinstance(value, dict) and value.get("WFSerializationType") == "WFTextTokenString":
                content = value["Value"]
                raw = content["string"].encode("utf-16-le")
                for span, ref in content.get("attachmentsByRange", {}).items():
                    offset = int(span.strip("{}").split(",")[0])
                    assert raw[offset * 2:(offset + 1) * 2] == "\ufffc".encode("utf-16-le")
                    assert ref["Type"] in ("ActionOutput", "Variable", "ExtensionInput")
        seen.add(p["UUID"])
        producers[p["UUID"]] = p
        if "WFControlFlowMode" in p:
            key = (a["WFWorkflowActionIdentifier"], p["GroupingIdentifier"])
            mode = p["WFControlFlowMode"]
            if mode == 0:
                stack.append(key)
            else:
                assert stack and stack[-1] == key, "Unbalanced control flow"
                if mode == 2:
                    stack.pop()
        if a["WFWorkflowActionIdentifier"].endswith("conditional") and p["WFControlFlowMode"] == 0:
            reference = p["WFInput"]["Variable"]["Value"]
            coercions = [v["CoercionItemClass"] for v in reference.get("Aggrandizements", [])
                         if v["Type"] == "WFCoercionVariableAggrandizement"]
            if "WFConditionalActionString" in p:
                assert coercions[-1:] == ["WFStringContentItem"], "Text comparison has an untyped input"
            if "WFNumberValue" in p:
                assert coercions[-1:] == ["WFNumberContentItem"], "Numeric comparison has an untyped input"
        if a["WFWorkflowActionIdentifier"].endswith("addnewevent"):
            assert p["WFCalendarItemShowComposeSheet"] is True
            assert p["ShowWhenRun"] is True, "Newer iOS versions also need the native compose flag"
            assert "WFCalendarItemCalendar" not in p, "Do not mix legacy and current calendar fields"
            if require_key:
                descriptor = p["WFCalendarDescriptor"]
                assert descriptor["WFSerializationType"] == "WFTextTokenAttachment", "Dynamic calendar names require an attachment, not a token string or embedded descriptor fields"
                assert descriptor["Value"]["Type"] == "Variable" and descriptor["Value"]["VariableName"] == "目标日历名称"
                assert descriptor["Value"]["Aggrandizements"][-1]["CoercionItemClass"] == "WFStringContentItem"
            else:
                assert "WFCalendarDescriptor" not in p
            if require_key or runtime_reminder:
                assert isinstance(p["WFAlertTime"], str), "Native alert enums must not use runtime references"
                assert p["WFAlertTime"] in dict(REMINDER_MODES)
                if p["WFAlertTime"] == "Custom":
                    assert p["WFAlertCustomTime"]["WFSerializationType"] == "WFTextTokenString"
                else:
                    assert "WFAlertCustomTime" not in p, "Off/start modes must not carry an empty custom alarm"
            else:
                if "WFAlertTime" in p:
                    assert p["WFAlertTime"] in ("30 minutes before", "Custom"), "Reminder probe must use a literal native alert, not a variable envelope"
                    if p["WFAlertTime"] == "Custom":
                        custom = p["WFAlertCustomTime"]
                        assert custom["WFSerializationType"] == "WFTextTokenString"
                        assert not custom["Value"]["attachmentsByRange"], "Custom probe must bypass date arithmetic and variables"
                        assert custom["Value"]["string"] == "2026-10-13T17:30:00+08:00"
                    else:
                        assert "WFAlertCustomTime" not in p, "Preset alarm probe must not contain a custom-time field"
        if a["WFWorkflowActionIdentifier"].endswith("gettimebetweendates"):
            operands = [k for k in ("WFDate", "WFTimeUntilFromDate", "WFTimeUntilCustomDate") if k in p]
            assert operands == ["WFTimeUntilFromDate"], "Native interval action requires exactly one start-date operand"
            assert "WFTimeUntilReferenceDate" not in p
            assert p["WFTimeUntilUnit"] == "Seconds"
            for key in ("WFInput", "WFTimeUntilFromDate"):
                assert p[key]["WFSerializationType"] == "WFTextTokenString", "Native interval dates are text parameters"
                attachments = list(p[key]["Value"]["attachmentsByRange"].values())
                assert len(attachments) == 1 and attachments[0]["Type"] == "ActionOutput"
        if a["WFWorkflowActionIdentifier"] in ("is.workflow.actions.format.date", "is.workflow.actions.adjustdate"):
            assert p["WFDate"]["WFSerializationType"] == "WFTextTokenString", "Date text slots require a token string, not a bare attachment"
            if p.get("WFDateFormatStyle") == "Custom":
                assert p["WFDateFormat"] in ("yyyy", "yyyy-MM-dd", "HH:mm:ss", "XXXXX", "yyyy-MM-dd HH:mm:ss XXX")
                assert "WFDateFormatString" not in p
            if p.get("WFAdjustOperation") == "Subtract":
                duration = p["WFDuration"]["Value"]
                assert duration["Unit"] == "min"
                assert duration["Magnitude"].get("Type") == "Variable", "Quantity magnitude requires a raw variable token"
                assert "WFSerializationType" not in duration["Magnitude"], "Quantity magnitude requires a raw variable token"
                assert duration["Magnitude"]["Aggrandizements"][-1]["CoercionItemClass"] == "WFNumberContentItem"
        assert a["WFWorkflowActionIdentifier"] != "is.workflow.actions.ask", "Runtime input windows must not precede the system calendar editor"
        if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.number":
            number = p["WFNumberActionNumber"]
            assert isinstance(number, str) or (isinstance(number, dict) and number.get("WFSerializationType") == "WFTextTokenAttachment"), "Native number fields require literal strings or attachments"
        if a["WFWorkflowActionIdentifier"].endswith("getvalueforkey") and p["WFDictionaryKey"] in ("title", "start", "end"):
            assert p["WFInput"]["Value"] == {"Type": "Variable", "VariableName": "Repeat Item"}, "Event fields must reference the current repeat item"
    assert not stack
    if require_key:
        question = api_key_question(workflow)
        p = workflow["WFWorkflowActions"][question["ActionIndex"]]["WFWorkflowActionParameters"]
        assert p[question["ParameterKey"]]["Value"]["string"] == PLACEHOLDER
        questions = workflow["WFWorkflowImportQuestions"]
        assert questions[0] == question, "The API key has one question before calendar setup"
        calendar_questions = [q for q in questions if q["ParameterKey"] == "WFGetUpcomingItemCalendar"]
        carriers = [(i, a) for i, a in enumerate(workflow["WFWorkflowActions"]) if a["WFWorkflowActionIdentifier"].endswith("getupcomingevents")]
        assert len(calendar_questions) == len(carriers) >= 1
        assert len(questions) == 1 + len(carriers), "Each calendar is selected once; reminder branches never have import questions"
        assert [q["ActionIndex"] for q in calendar_questions] == [i for i, a in carriers]
        for question, (index, carrier) in zip(calendar_questions, carriers):
            assert question["Category"] == "Parameter" and question["DefaultValue"] == {"IsAllCalendar": True}
            assert carrier["WFWorkflowActionParameters"][question["ParameterKey"]] == question["DefaultValue"]
            assert carrier["WFWorkflowActionParameters"]["CustomOutputName"] == CALENDAR_CARRIER_NAME
        assert [i for i, a in carriers] == list(range(4, 4 + len(carriers))), "All calendar carriers stay inside the one false setup block"
        gate = workflow["WFWorkflowActions"][3]["WFWorkflowActionParameters"]
        assert gate["WFCondition"] == 4 and gate["WFNumberValue"] == 1
        assert workflow["WFWorkflowActions"][2]["WFWorkflowActionParameters"]["WFNumberActionNumber"] == "0"
        assert gate["WFInput"]["Variable"]["Value"]["OutputUUID"] == workflow["WFWorkflowActions"][2]["WFWorkflowActionParameters"]["UUID"]
        assert sum(a["WFWorkflowActionIdentifier"].endswith("addnewevent") for a in workflow["WFWorkflowActions"]) == 3
    elif calendar_import_probe:
        question, = workflow["WFWorkflowImportQuestions"]
        assert question["Category"] == "Parameter" and question["ParameterKey"] == "WFGetUpcomingItemCalendar"
        assert question["DefaultValue"] == {"IsAllCalendar": True}, "Probe uses a visible range, never an empty title/ID descriptor"
        carrier = workflow["WFWorkflowActions"][question["ActionIndex"]]
        assert carrier["WFWorkflowActionIdentifier"] == "is.workflow.actions.getupcomingevents"
        assert carrier["WFWorkflowActionParameters"][question["ParameterKey"]] == question["DefaultValue"]
        assert sum(a["WFWorkflowActionIdentifier"].endswith("getupcomingevents") for a in workflow["WFWorkflowActions"]) == 1
        assert not any(a["WFWorkflowActionIdentifier"].endswith("choosefrommenu") for a in workflow["WFWorkflowActions"])
    elif calendar_selection_probe:
        assert not workflow["WFWorkflowImportQuestions"], "Do not recreate the blank import-calendar row observed on iOS 26.6"
        carriers = [a["WFWorkflowActionParameters"] for a in workflow["WFWorkflowActions"]
                    if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.getupcomingevents"]
        assert len(carriers) == 2
        assert carriers[0]["WFGetUpcomingItemCalendar"] == {"IsAllCalendar": True}
        ask = carriers[1]["WFGetUpcomingItemCalendar"]
        assert ask["WFSerializationType"] == "WFTextTokenAttachment" and ask["Value"]["Type"] == "Ask"
        assert all(p["WFGetUpcomingItemCount"] == 1 and p["WFDateSpecifier"] == "Today" for p in carriers)
    else:
        assert not workflow["WFWorkflowImportQuestions"]


def walk(value):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


class Stopped(Exception):
    pass


class FixtureRunner:
    """Evaluate serialized references/branches against fixed responses, not Apple APIs."""
    def __init__(self, workflow, shared=None, events=None, api=None, empty_outputs=(), calendar_bindings=None, source_app="", editor_delay=0, shortcut_library=None, selection_mode=2, upcoming_fixture=None, cancel_picker=False, selected_calendars=None, action_delays=None):
        self.actions = copy.deepcopy(workflow["WFWorkflowActions"])
        self.shared = shared
        self.outputs, self.variables = {}, {}
        self.requests, self.calendar, self.alerts, self.questions = [], [], [], []
        self.empty_outputs = set(empty_outputs)
        self.producers = {a["WFWorkflowActionParameters"]["UUID"]: a["WFWorkflowActionParameters"] for a in self.actions}
        self.screenshots = 0
        self.editor_delay = editor_delay
        self.action_delays = action_delays or {}
        self.shortcut_library = shortcut_library
        self.source_exports = []
        self.shortcut_output = None
        self.selection_mode = selection_mode
        self.upcoming_fixture = upcoming_fixture
        self.cancel_picker = cancel_picker
        self.calendar_queries, self.menus = [], []
        self.current = dt.datetime(2026, 10, 1, 13, 17, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.api = api or {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"source_app": source_app, "events": events or []})}}]}
        if any(q["ParameterKey"] == "WFTextActionText" for q in workflow["WFWorkflowImportQuestions"]):
            key_action = self.actions[api_key_question(workflow)["ActionIndex"]]
            key_action["WFWorkflowActionParameters"]["WFTextActionText"]["Value"]["string"] = "  fixture-key-not-real  "
            # Explicit sample XML models stored selections, not discovery or
            # native title resolution. The executed carrier stays disabled.
            configured = copy.deepcopy(workflow)
            configured["WFWorkflowActions"][api_key_question(workflow)["ActionIndex"]] = copy.deepcopy(key_action)
            positions = [i for i, a in enumerate(self.actions) if a["WFWorkflowActionParameters"].get("CustomOutputName") == CALENDAR_CARRIER_NAME]
            position = positions[0]
            bindings = selected_calendars if selected_calendars is not None else [
                (calendar_bindings or {}).get(category, {"Title": category, "Identifier": "fixture-calendar-" + category, "IsAllCalendar": False})
                for category in SAMPLE_CALENDARS]
            carriers = []
            for index, binding in enumerate(bindings):
                if binding is None:
                    continue
                carrier = copy.deepcopy(self.actions[position])
                carrier["WFWorkflowActionParameters"]["WFGetUpcomingItemCalendar"] = binding
                carrier["WFWorkflowActionParameters"]["UUID"] = f"00000000-0000-0000-0000-{index+1:012d}"
                carriers.append(carrier)
            # Explicit hypothetical installed choices; this is not an import UI
            # simulation. Rebind source indices when the fixture changes count.
            carriers = carriers or [copy.deepcopy(self.actions[position])]
            configured["WFWorkflowActions"][position:positions[-1]+1] = carriers
            template = next(q for q in configured["WFWorkflowImportQuestions"] if q["ParameterKey"] == "WFGetUpcomingItemCalendar")
            configured["WFWorkflowImportQuestions"] = [configured["WFWorkflowImportQuestions"][0],
                *[{**template, "ActionIndex": position + i} for i in range(len(carriers))]]
            if shortcut_library is None:
                self.shortcut_library = [{"fixture_name": "其他快捷指令", "fixture_xml": "MUST NOT BE EXPORTED"},
                                         {"fixture_name": FILE_STEM, "fixture_xml": plistlib.dumps(configured, fmt=plistlib.FMT_XML, sort_keys=False).decode()}]
        self.groups = {}
        stack = []
        for i, a in enumerate(self.actions):
            p = a["WFWorkflowActionParameters"]
            if "WFControlFlowMode" not in p:
                continue
            if p["WFControlFlowMode"] == 0:
                stack.append([i, [] if a["WFWorkflowActionIdentifier"].endswith("choosefrommenu") else None])
            elif p["WFControlFlowMode"] == 1:
                if isinstance(stack[-1][1], list):
                    stack[-1][1].append(i)
                else:
                    stack[-1][1] = i
            else:
                start, otherwise = stack.pop()
                self.groups[start] = (otherwise, i)

    def resolve(self, value):
        if not isinstance(value, dict):
            return value
        serial = value.get("WFSerializationType")
        if serial == "WFTextTokenAttachment":
            return self.resolve(value["Value"])
        if serial == "WFTextTokenString":
            data = value["Value"]
            raw = data["string"].encode("utf-16-le")
            pieces, position = [], 0
            attachments = sorted(data.get("attachmentsByRange", {}).items(),
                                 key=lambda pair: int(pair[0].strip("{}").split(",")[0]))
            for span, ref in attachments:
                offset = int(span.strip("{}").split(",")[0]) * 2
                resolved = self.resolve(ref)
                def render(item):
                    return json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else "" if item is None else str(item)
                rendered = "\n".join(render(item) for item in resolved) if isinstance(resolved, list) else render(resolved)
                pieces += [raw[position:offset].decode("utf-16-le"), rendered]
                position = offset + 2
            pieces.append(raw[position:].decode("utf-16-le"))
            return "".join(pieces)
        if serial == "WFDictionaryFieldValue":
            if value["Value"].get("WFSerializationType"):
                return self.resolve(value["Value"])
            return {self.resolve(item["WFKey"]): self.decode_field(item)
                    for item in value["Value"]["WFDictionaryFieldValueItems"]}
        if serial == "WFArrayParameterState":
            return [self.decode_field(item) for item in value["Value"]]
        if serial == "WFNumberSubstitutableState":
            return value["Value"]
        kind = value.get("Type")
        if kind == "ActionOutput":
            assert value["OutputName"] == self.producers[value["OutputUUID"]]["CustomOutputName"]
            resolved = self.outputs[value["OutputUUID"]]
        elif kind == "Variable":
            resolved = self.variables.get(value["VariableName"])
        elif kind == "ExtensionInput":
            resolved = self.shared
        else:
            return value
        for coercion in value.get("Aggrandizements", []):
            assert coercion["Type"] == "WFCoercionVariableAggrandizement"
            class_name = coercion["CoercionItemClass"]
            if class_name == "WFStringContentItem":
                resolved = "" if resolved is None else str(resolved)
            elif class_name == "WFNumberContentItem":
                resolved = float(resolved)
            elif class_name == "WFBooleanContentItem":
                resolved = bool(resolved)
            else:
                raise AssertionError(f"Unhandled coercion {class_name}")
        return resolved

    def decode_field(self, item):
        value = self.resolve(item["WFValue"])
        return float(value) if item["WFItemType"] == 3 else value

    def condition(self, p):
        value = self.resolve(p["WFInput"]["Variable"])
        condition = p["WFCondition"]
        if condition in (100, 101):
            present = value is not None and value != "" and value != [] and value != {}
            return present if condition == 100 else not present
        comparison = self.resolve(p.get("WFNumberValue", p.get("WFConditionalActionString")))
        if condition == 4:
            return value == comparison
        if condition == 1:
            return value <= comparison
        raise AssertionError(f"Unhandled condition {condition}")

    def date(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, dt.datetime):
            return value
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=self.current.tzinfo)

    def run_range(self, start, end):
        i = start
        while i < end:
            a = self.actions[i]
            name = a["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")
            p = a["WFWorkflowActionParameters"]
            result = None
            # Explicit fake device delays verify diagnostic attribution, not
            # real iOS speed. Conditional/loop nodes have no injected delay.
            self.current += dt.timedelta(seconds=self.action_delays.get(p.get("CustomOutputName"),
                                                                       self.action_delays.get(name, 0)))
            if name == "choosefrommenu":
                cases, close = self.groups[i]
                choices = [self.resolve(item["WFValue"]) for item in p["WFMenuItems"]]
                self.menus.append(choices)
                assert 1 <= self.selection_mode <= len(cases) == len(choices)
                selected_case = cases[self.selection_mode - 1]
                assert self.resolve(self.actions[selected_case]["WFWorkflowActionParameters"]["WFMenuItemTitle"]) == choices[self.selection_mode - 1]
                self.run_range(selected_case + 1, cases[self.selection_mode] if self.selection_mode < len(cases) else close)
                i = close + 1
                continue
            if name == "conditional":
                alternate, close = self.groups[i]
                if self.condition(p):
                    self.run_range(i + 1, alternate if alternate is not None else close)
                elif alternate is not None:
                    self.run_range(alternate + 1, close)
                i = close + 1
                continue
            if name == "repeat.each":
                _, close = self.groups[i]
                previous = self.variables.get("Repeat Item")
                for item in self.resolve(p["WFInput"]):
                    self.variables["Repeat Item"] = item
                    self.run_range(i + 1, close)
                self.variables["Repeat Item"] = previous
                i = close + 1
                continue
            if name == "gettext":
                result = self.resolve(p["WFTextActionText"])
            elif name == "number":
                result = float(self.resolve(p["WFNumberActionNumber"]))
            elif name == "setvariable":
                self.variables[p["WFVariableName"]] = self.resolve(p["WFInput"])
            elif name == "appendvariable":
                old = self.variables.get(p["WFVariableName"], [])
                old = list(old) if isinstance(old, list) else [old]
                item = self.resolve(p["WFInput"])
                old.extend(item if isinstance(item, list) else [item])
                self.variables[p["WFVariableName"]] = old
            elif name == "list":
                result = [self.decode_field(item) for item in p["WFItems"]]
            elif name == "dictionary":
                result = self.resolve(p["WFItems"])
            elif name == "setvalueforkey":
                assert "WFInput" not in p, "Set Dictionary Value uses its explicit WFDictionary slot"
                result = dict(self.resolve(p["WFDictionary"]))
                result[self.resolve(p["WFDictionaryKey"])] = self.resolve(p["WFDictionaryValue"])
            elif name == "text.replace":
                # Foundation regex replacements use $1, whereas Python uses \g<1>.
                replacement = re.sub(r"\$(\d+)", lambda m: "\\g<" + m[1] + ">", p["WFReplaceTextReplace"])
                source = self.resolve(p["WFInput"])
                result = (re.sub(p["WFReplaceTextFind"], replacement, source)
                          if p.get("WFReplaceTextRegularExpression") else
                          source.replace(p["WFReplaceTextFind"], p["WFReplaceTextReplace"]))
            elif name == "takescreenshot":
                self.screenshots += 1
                result = {"fixture_image": "screen"}
            elif name == "detect.images":
                value = self.resolve(p["WFInput"])
                result = [value] if isinstance(value, dict) and "fixture_image" in value else []
            elif name == "detect.text":
                result = self.resolve(p["WFInput"])
                if isinstance(result, dict) and "fixture_data" in result:
                    result = result["fixture_data"]
            elif name == "getmyworkflows":
                assert self.shortcut_library is not None, "Native library/export results must be explicitly supplied; this is not a device test"
                result = self.shortcut_library
            elif name == "getitemname":
                result = self.resolve(p["WFInput"])["fixture_name"]
            elif name == "gettypeaction":
                item = self.resolve(p["WFInput"])
                assert p["WFFileType"] == "com.apple.shortcuts.workflow-file"
                self.source_exports.append(item["fixture_name"])
                result = {"fixture_data": item["fixture_xml"]}
            elif name == "setitemname":
                result = {**self.resolve(p["WFInput"]), "fixture_name": self.resolve(p["WFName"])}
            elif name == "count":
                assert p["WFCountType"] == "Items"
                result = len(self.resolve(p["Input"]))
            elif name == "getupcomingevents":
                assert p["CustomOutputName"] not in ("不执行的日历参数载体", CALENDAR_CARRIER_NAME), "The selection probe executed its disabled calendar carrier"
                assert p["WFGetUpcomingItemCalendar"]["Value"]["Type"] == "Ask"
                assert p["WFGetUpcomingItemCount"] == 1 and p["WFDateSpecifier"] == "Today"
                assert self.upcoming_fixture is not None, "Read-only query results must be explicitly supplied; this does not test a native picker"
                self.calendar_queries.append(p)
                if self.cancel_picker:
                    raise Stopped()
                result = self.upcoming_fixture
            elif name == "getitemfromlist":
                result = self.resolve(p["WFInput"])[0]
            elif name == "image.convert":
                result = "Zml4dHVyZQ=="
            elif name == "base64encode":
                raw = self.resolve(p["WFInput"])
                textual = bool(p["WFInput"].get("Value", {}).get("Aggrandizements"))
                result = base64.b64encode(raw.encode("utf-8")).decode("ascii") if textual else "Zml4dHVyZQ=="
            elif name == "date":
                result = self.current if p["WFDateActionMode"] == "Current Date" else self.date(self.resolve(p["WFDateActionDate"]))
            elif name == "format.date":
                # The native text slot does not read a bare token attachment.
                value = self.date(self.resolve(p["WFDate"])) if p["WFDate"].get("WFSerializationType") == "WFTextTokenString" else None
                if value is None:
                    result = None
                elif p["WFDateFormatStyle"] == "Custom":
                    value = value.astimezone(self.current.tzinfo)
                    pattern = p["WFDateFormat"]
                    formats = {"yyyy": "%Y", "yyyy-MM-dd": "%Y-%m-%d", "HH:mm:ss": "%H:%M:%S"}
                    if pattern in ("XXXXX", "yyyy-MM-dd HH:mm:ss XXX"):
                        offset = value.strftime("%z")
                        result = "Z" if offset == "+0000" else offset[:3] + ":" + offset[3:]
                        if pattern != "XXXXX":
                            result = value.strftime("%Y-%m-%d %H:%M:%S ") + result
                    else:
                        result = value.strftime(formats[pattern])
                else:
                    assert p["WFDateFormatStyle"] == "ISO 8601" and p["WFISO8601IncludeTime"] is True
                    result = value.isoformat()
            elif name == "downloadurl":
                headers, body = self.resolve(p["WFHTTPHeaders"]), self.resolve(p["WFJSONValues"])
                assert headers["Authorization"] == "Bearer fixture-key-not-real"
                assert p["WFURL"] == ENDPOINT
                assert body["model"] == MODEL and body["thinking"]["type"] == "enabled"
                assert body["reasoning_effort"] == "high"
                assert body["stream"] is False and body["max_tokens"] == 16384
                assert body["response_format"] == {"type": "json_object"}
                self.requests.append(body)
                result = self.api
            elif name == "detect.dictionary":
                value = self.resolve(p["WFInput"])
                result = json.loads(value) if isinstance(value, str) else value
            elif name == "getvalueforkey":
                result = self.resolve(p["WFInput"]).get(self.resolve(p["WFDictionaryKey"]), "")
            elif name == "text.match":
                result = re.findall(self.resolve(p["WFMatchTextPattern"]), self.resolve(p["text"]))
            elif name == "adjustdate":
                value = self.date(self.resolve(p["WFDate"]))
                if value is None:
                    result = None
                elif p["WFAdjustOperation"] == "Get Start of Day":
                    result = value.astimezone(self.current.tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    duration = p["WFDuration"]["Value"]
                    magnitude = float(self.resolve(duration["Magnitude"]))
                    if p["WFAdjustOperation"] == "Subtract":
                        magnitude = -magnitude
                    result = value + dt.timedelta(**{{"hr": "hours", "days": "days", "min": "minutes"}[duration["Unit"]]: magnitude})
            elif name == "gettimebetweendates":
                result = (self.date(self.resolve(p["WFInput"])) - self.date(self.resolve(p["WFTimeUntilFromDate"]))).total_seconds()
            elif name == "ask":
                raise AssertionError("Unexpected input window before calendar compose")
            elif name == "alert":
                self.alerts.append(self.resolve(p["WFAlertActionMessage"]))
            elif name == "exit":
                raise Stopped()
            elif name == "output":
                self.shortcut_output = self.resolve(p["WFOutput"])
                raise Stopped()
            elif name == "addnewevent":
                self.calendar.append({k: self.resolve(v) for k, v in p.items()
                                      if k.startswith("WFCalendarItem") or k in ("ShowWhenRun", "WFCalendarDescriptor", "WFAlertTime", "WFAlertCustomTime")})
                self.current += dt.timedelta(seconds=self.editor_delay)
            elif name != "comment":
                raise AssertionError(f"Unhandled action {name}")
            if name in self.empty_outputs or p["UUID"] in self.empty_outputs:
                result = None
            self.outputs[p["UUID"]] = result
            i += 1

    def run(self):
        try:
            self.run_range(0, len(self.actions))
        except Stopped:
            pass
        return self


def main(calendar_slots=DEFAULT_CALENDAR_SLOTS):
    write(calendar_slots)
    workflow = build(calendar_slots)
    structure(workflow)
    report = []

    def editor(run, index=0):
        assert not run.questions and not run.alerts, run.alerts
        event = run.calendar[index]
        assert event["WFCalendarItemShowComposeSheet"] is event["ShowWhenRun"] is True
        assert "WFCalendarItemCalendar" not in event and isinstance(event["WFCalendarDescriptor"], str)
        assert event["WFCalendarDescriptor"] in run.variables["可选日历名称表"].values()
        assert event["WFAlertTime"] in ("None", "At time of event", "5 minutes before", "15 minutes before", "30 minutes before", "1 hour before", "2 hours before", "1 day before", "2 days before", "1 week before", "Custom")
        metadata = event["WFCalendarItemNotes"].split("\n\n原文：\n")[0]
        assert all(label not in metadata for label in ("需核对：", "预填说明：", "预填年份", "原返回开始", "原返回结束"))
        assert run.date(event["WFCalendarItemEndDate"]) > run.date(event["WFCalendarItemStartDate"])
        return event

    legacy_interval = copy.deepcopy(workflow)
    for a in legacy_interval["WFWorkflowActions"]:
        if a["WFWorkflowActionIdentifier"].endswith("gettimebetweendates"):
            p = a["WFWorkflowActionParameters"]
            p["WFTimeUntilCustomDate"] = p["WFTimeUntilFromDate"]
            break
    try:
        structure(legacy_interval)
    except AssertionError as error:
        assert "exactly one" in str(error)
    else:
        raise AssertionError("Conflicting native date operands were accepted")
    report.append("Native contract rejects the old conflicting start-date operands; both interval inputs use explicit ISO text references")

    base = {"title": "项目会议", "start": "2026-10-02T14:00:00+08:00",
            "end": "2026-10-02T15:00:00+08:00", "all_day": False,
            "location": "会议室A", "notes": "讨论计划", "source_text": "10月2日14到15点",
            "needs_review": False, "review_reason": ""}

    for slots in (1, 3, 9):
        customized = build(slots)
        structure(customized)
        install_questions = customized["WFWorkflowImportQuestions"][1:]
        assert len(install_questions) == slots and len({q["ActionIndex"] for q in install_questions}) == slots
        installed = copy.deepcopy(customized)
        selected_names = []
        for index, question in enumerate(install_questions):
            # Supply hypothetical saved native answers at the ACTUAL import
            # indices. This covers installation-to-readback wiring, not iOS UI.
            if slots > 1 and index == 0:
                continue
            title = f"候选 {index} & <专题>.2026"
            selected_names.append(title)
            parameters = installed["WFWorkflowActions"][question["ActionIndex"]]["WFWorkflowActionParameters"]
            parameters[question["ParameterKey"]] = {"IsAllCalendar": False, "Title": title, "Identifier": f"fixture-installed-{index}"}
        xml = plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=False).decode()
        answers = [dict(base, calendar_category=name) for name in selected_names]
        answers.append(dict(base, calendar_category="not selected"))
        supplied = FixtureRunner(customized, events=answers,
                                 shortcut_library=[{"fixture_name": FILE_STEM, "fixture_xml": xml}]).run()
        assert len(supplied.requests) == 1 and len(supplied.calendar) == len(answers)
        assert list(supplied.variables["可选日历名称表"].values()) == selected_names
        assert [editor(supplied, i)["WFCalendarDescriptor"] for i in range(len(answers))] == [*selected_names, selected_names[0]]
        assert not supplied.calendar_queries and not supplied.questions and not supplied.alerts
    report.append("Production exposes one native installation question per candidate, defaults to All Calendars without fabricated IDs, and supports 1/3/9 configured slots; supplied answers at actual action indices read back in order, skip unused ranges, classify independently and fall back to the first concrete selection without editing or runtime queries; this does not simulate native multi-question UI")
    for invalid_slots in (0, -1, True, "3", None):
        try:
            build(invalid_slots)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid candidate count was accepted")
    assert workflow == build(calendar_slots)
    report.append("Candidate count validation rejects empty/invalid builds before generating actions; custom builds stay deterministic and isolated from diagnostics, and calendar labels remain device selections rather than five fixed classes")

    screenshot = FixtureRunner(workflow, events=[base]).run()
    assert screenshot.screenshots == 1 and len(screenshot.requests) == len(screenshot.calendar) == 1
    content = screenshot.requests[0]["messages"][1]["content"]
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,Zml4dHVyZQ=="
    assert content[1]["image_url"]["detail"] == "high"
    assert screenshot.calendar[0]["WFCalendarItemLocation"] == "会议室A"
    event = screenshot.calendar[0]
    assert event["WFCalendarItemTitle"] == base["title"]
    assert event["WFCalendarItemStartDate"] == "2026-10-02T14:00:00+08:00"
    assert event["WFCalendarItemEndDate"] == "2026-10-02T15:00:00+08:00"
    assert base["notes"] in event["WFCalendarItemNotes"] and base["source_text"] in event["WFCalendarItemNotes"]
    assert event["WFCalendarItemAllDay"] is False
    assert event["WFCalendarItemShowComposeSheet"] is event["ShowWhenRun"] is True
    assert not screenshot.questions
    report.append("No-input branch captures a screenshot and builds one image request")
    report.append("Every event reaches one editable native compose sheet; three literal reminder branches share the same dynamic selected calendar name")

    shared = FixtureRunner(workflow, shared="会议资料 \\\"quoted\\\"\n第二行", events=[base, dict(base, title="第二条")]).run()
    assert shared.screenshots == 0 and len(shared.calendar) == 2 and len(shared.requests) == 1
    assert "第二行" in shared.requests[0]["messages"][1]["content"]
    json.loads(json.dumps(shared.requests[0], ensure_ascii=False))
    report.append("Shared text preserves quotes/newlines; event fields use the current Repeat Item, with multiple events in one API request")

    shared_image = FixtureRunner(workflow, shared={"fixture_image": "photo"}, events=[base]).run()
    assert shared_image.screenshots == 0 and len(shared_image.calendar) == 1
    report.append("Shared image uses the image branch without taking a new screenshot")

    thinking_api = {"choices": [{"finish_reason": "stop", "message": {
        "reasoning_content": "模拟思考，不是最终 JSON：2030年1月1日的日期不应写入日历。",
        "content": json.dumps({"events": [base]})}}]}
    thinking_run = FixtureRunner(workflow, api=thinking_api).run()
    assert len(thinking_run.calendar) == 1 and not thinking_run.alerts
    assert thinking_run.calendar[0]["WFCalendarItemTitle"] == base["title"]
    assert thinking_run.calendar[0]["WFCalendarItemStartDate"] == "2026-10-02T14:00:00+08:00"
    for empty_content in ("", None):
        only_thinking_api = copy.deepcopy(thinking_api)
        only_thinking_api["choices"][0]["message"]["content"] = empty_content
        only_thinking_run = FixtureRunner(workflow, api=only_thinking_api).run()
        assert not only_thinking_run.calendar and not only_thinking_run.questions
        assert only_thinking_run.alerts
    report.append("Thinking responses use only final content; reasoning without a final answer never creates an event")

    food = dict(base, title="和朋友吃饭", start="10-13T18:00:00+08:00", end="",
                source_text="10月13号下午6:00和朋友吃饭", needs_review=True,
                review_reason="原文未注明年份和结束时间")
    for missing_end in ("", None, " \n "):
        run = FixtureRunner(workflow, events=[dict(food, end=missing_end)]).run()
        event = editor(run)
        assert event["WFCalendarItemStartDate"] == "2026-10-13T18:00:00+08:00"
        assert event["WFCalendarItemEndDate"] == "2026-10-13T19:00:00+08:00"
        notes = event["WFCalendarItemNotes"]
        assert "预填" not in notes and "无法读取" not in notes
        assert food["source_text"] in notes and "已确认年份" not in notes
    absent_end = dict(food)
    del absent_end["end"]
    assert editor(FixtureRunner(workflow, events=[absent_end]).run())["WFCalendarItemEndDate"] == "2026-10-13T19:00:00+08:00"
    report.append("Dinner example preserves October 13 18:00 and prefills 19:00; absent, null, empty and whitespace end values use the one-hour default without review notes or intermediate windows")

    yearless = dict(base, title="中水站参观", start="10-08T14:00:00+08:00", end="10-08T14:30:00+08:00",
                    source_text="中水站参观安排在10.8下午14:00-14:30", needs_review=True,
                    review_reason="原文未注明年份")
    yearless_run = FixtureRunner(workflow, events=[yearless]).run()
    event = editor(yearless_run)
    assert event["WFCalendarItemStartDate"] == "2026-10-08T14:00:00+08:00"
    assert event["WFCalendarItemEndDate"] == "2026-10-08T14:30:00+08:00"
    new_year = FixtureRunner(workflow, events=[yearless])
    new_year.current = new_year.current.replace(year=2027, month=1, day=1)
    assert editor(new_year.run())["WFCalendarItemStartDate"] == "2027-10-08T14:00:00+08:00"
    known_year = FixtureRunner(workflow, events=[dict(base, start="2028-10-02T14:00:00+08:00", end="2028-10-02T15:00:00+08:00")]).run()
    assert editor(known_year)["WFCalendarItemStartDate"].startswith("2028-")
    assert "预填年份" not in known_year.calendar[0]["WFCalendarItemNotes"]
    report.append("Missing years use the runtime year while preserving month/day and clocks; explicit years remain unchanged, including after a device-year rollover")

    unknown_start = FixtureRunner(workflow, events=[dict(base, title=None, start=None, end=None)]).run()
    event = editor(unknown_start)
    assert event["WFCalendarItemTitle"] == "待确认日程"
    assert event["WFCalendarItemStartDate"] == "2026-10-01T13:17:00+08:00"
    assert event["WFCalendarItemEndDate"] == "2026-10-01T14:17:00+08:00"
    report.append("Missing title/start/end directly prefill the editable title and runtime date/time, plus one hour, without review sections in notes")

    for start in ("2026-10-13", "10-13"):
        event = editor(FixtureRunner(workflow, events=[dict(base, start=start, end="")]).run())
        assert event["WFCalendarItemStartDate"] == "2026-10-13T13:17:00+08:00"
        assert event["WFCalendarItemEndDate"] == "2026-10-13T14:17:00+08:00"
    event = editor(FixtureRunner(workflow, events=[dict(base, start="2026-10-13T18:00:00+08:00", end="2026-10-14")]).run())
    assert event["WFCalendarItemEndDate"] == "2026-10-14T19:00:00+08:00"
    report.append("Date-only timed values retain their known date; only the missing clock is filled, including a known end date")

    for start in ("T18:00:00+08:00", "18:00", "18:00+0800"):
        event = editor(FixtureRunner(workflow, events=[dict(base, start=start, end="T19:00:00+08:00")]).run())
        assert event["WFCalendarItemStartDate"] == "2026-10-01T18:00:00+08:00"
        assert event["WFCalendarItemEndDate"] == "2026-10-01T19:00:00+08:00"
    event = editor(FixtureRunner(workflow, events=[dict(base, start="2026-10-13T18:00:00+08:00", end="T19:00+08:00")]).run())
    assert event["WFCalendarItemEndDate"] == "2026-10-13T19:00:00+08:00"
    report.append("Time-only values retain known clocks; missing start dates use the device day and missing end dates use the start day")

    for start in ("Friday", "2026-13-02T14:00:00+08:00", "2026-10-02T25:00:00+08:00"):
        event = editor(FixtureRunner(workflow, events=[dict(base, start=start, end="bad-end")]).run())
        assert event["WFCalendarItemStartDate"] == "2026-10-01T13:17:00+08:00"
        assert event["WFCalendarItemEndDate"] == "2026-10-01T14:17:00+08:00"
    report.append("Unreadable values retain the 0.1.7 date fallback without questions or technical review notes")

    for end in (base["start"], "2026-10-02T13:00:00+08:00"):
        event = editor(FixtureRunner(workflow, events=[dict(base, end=end)]).run())
        assert event["WFCalendarItemStartDate"] == base["start"]
        assert event["WFCalendarItemEndDate"] == "2026-10-02T15:00:00+08:00"
    crossing = dict(yearless, start="12-31T23:00:00+08:00", end="01-01T01:00:00+08:00")
    event = editor(FixtureRunner(workflow, events=[crossing]).run())
    assert event["WFCalendarItemEndDate"] == "2027-01-01T00:00:00+08:00"
    report.append("Equal or reversed times prefill start plus one hour without writing technical return values to notes; yearless cross-year conflicts follow the same explicit default rule")

    for start in ("2026-10-13", "10-13"):
        for end, expected_day in ((None, 14), ("", 14), ("2026-10-15", 15), ("10-15", 15), (start, 14)):
            event = editor(FixtureRunner(workflow, events=[dict(base, start=start, end=end, all_day=True)]).run())
            assert event["WFCalendarItemAllDay"] is True
            assert event["WFCalendarItemStartDate"] == "2026-10-13T00:00:00+08:00"
            assert event["WFCalendarItemEndDate"] == f"2026-10-{expected_day}T00:00:00+08:00"
    event = editor(FixtureRunner(workflow, events=[dict(base, start="", end="", all_day=True)]).run())
    assert event["WFCalendarItemStartDate"] == "2026-10-01T00:00:00+08:00"
    assert event["WFCalendarItemEndDate"] == "2026-10-02T00:00:00+08:00"
    report.append("All-day events use midnight and an exclusive end; missing or conflicting ends prefill one day, while explicit valid end dates are retained")

    multiple = FixtureRunner(workflow, shared="多条资料", events=[food, base, dict(base, title="第三条", start="T09:00:00+08:00", end="")]).run()
    assert len(multiple.requests) == 1 and len(multiple.calendar) == 3
    assert "预填年份" not in editor(multiple, 0)["WFCalendarItemNotes"]
    assert "预填说明" not in editor(multiple, 1)["WFCalendarItemNotes"]
    third = editor(multiple, 2)
    assert "预填年份" not in third["WFCalendarItemNotes"]
    assert third["WFCalendarItemTitle"] == "第三条"
    assert "多条资料" in third["WFCalendarItemNotes"]
    report.append("Multiple events each receive their own editable sheet; dates, titles, sources and classifications do not leak between repeat items")

    flagged = FixtureRunner(workflow, events=[dict(base, needs_review=True)]).run()
    assert "不确定信息" not in editor(flagged)["WFCalendarItemNotes"]
    assert "需核对" not in editor(screenshot)["WFCalendarItemNotes"]
    report.append("Legacy review fields do not add review notes or a confirmation alert")

    visit = dict(base, title=" 中水站参观 \n", start="2026-10-08T14:00:00+08:00",
                 end="2026-10-08T14:30:00+08:00", location="", notes="",
                 source_text="中水站参观安排在10.8下午14:00-14:30", needs_review=True,
                 review_reason=" \n ")
    visit_run = FixtureRunner(workflow, shared="参观安排", events=[visit]).run()
    visit_editor = editor(visit_run)
    assert visit_editor["WFCalendarItemTitle"] == "中水站参观"
    assert visit_editor["WFCalendarItemStartDate"] == "2026-10-08T14:00:00+08:00"
    assert visit_editor["WFCalendarItemEndDate"] == "2026-10-08T14:30:00+08:00"
    assert "需核对" not in visit_editor["WFCalendarItemNotes"]
    assert "原文：\n参观安排" in visit_editor["WFCalendarItemNotes"]
    assert not visit_editor["WFCalendarItemNotes"].startswith("\n")
    no_context = FixtureRunner(workflow, events=[dict(base, notes="具体描述", source_text="", review_reason=" \n ")]).run()
    assert "原文：\n（未提取到原文）" in editor(no_context)["WFCalendarItemNotes"]
    assert editor(no_context)["WFCalendarItemNotes"].endswith("活动备注：\n具体描述")
    report.append("Visit example keeps separate 14:00/14:30 fields; whitespace and empty review/default/source headings are omitted")

    for start, end in (
        ("2026-10-08T14:00+08:00", "2026-10-08T14:30+08:00"),
        ("2026-10-08 14:00:00+0800", "2026-10-08 14:30:00+0800"),
        ("2026-10-08t14:00:00.000+08:00", "2026-10-08t14:30:00.000+08:00"),
        ("2026-10-08T06:00Z", "2026-10-08T06:30Z"),
        ("10-08T14:00+0800", "10-08T14:30+0800"),
        ("2026-10-08T14:00", "2026-10-08T14:30"),
    ):
        varied = FixtureRunner(workflow, events=[dict(visit, start=start, end=end)]).run()
        event = editor(varied)
        expected = dt.datetime(2026, 10, 8, 14, 0, tzinfo=varied.current.tzinfo)
        assert varied.date(event["WFCalendarItemStartDate"]) == expected
        assert varied.date(event["WFCalendarItemEndDate"]) == expected + dt.timedelta(minutes=30)
    report.append("Equivalent ISO spellings preserve October 8 14:00-14:30 despite device time October 1 13:17; absent timezone uses the device offset")

    tz_run = FixtureRunner(workflow, events=[dict(base, start="10-13T18:00", end="")])
    tz_run.current = tz_run.current.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5, minutes=-30)))
    event = editor(tz_run.run())
    assert event["WFCalendarItemStartDate"] == "2026-10-13T18:00:00-05:30"
    assert event["WFCalendarItemEndDate"] == "2026-10-13T19:00:00-05:30"
    report.append("Runtime device date/time formatting supports a non-China timezone and keeps the local recognized clock")

    categories = FixtureRunner(workflow, events=[dict(base, title=name, calendar_category=name)
                                                for name in SAMPLE_CALENDARS]).run()
    assert len(categories.requests) == 1 and len(categories.calendar) == 5
    for index, category in enumerate(SAMPLE_CALENDARS):
        event = editor(categories, index)
        assert event["WFCalendarDescriptor"] == category
        assert event["WFAlertTime"] == "Custom"
        assert categories.date(event["WFAlertCustomTime"]) == categories.date(event["WFCalendarItemStartDate"]) - dt.timedelta(minutes=30)
    for category in ("", None, "其他", "生活和学习", " \n "):
        event = editor(FixtureRunner(workflow, events=[dict(base, calendar_category=category)]).run())
        assert event["WFCalendarDescriptor"] == "生活"
    for phrase in ("课程作业优先对应作业", "课题实验优先对应科研", "课程参观优先对应学习", "非课程讲座", "候选数量不限于五类", "默认日历"):
        assert phrase in SYSTEM_PROMPT
    report.append("Five sample names route through one recognition request and three literal reminder branches; unknown/empty categories use the first configured calendar; actual candidate names replace fixed categories")

    missing_binding = FixtureRunner(workflow, events=[dict(base, calendar_category="学习")], calendar_bindings={"学习": None}).run()
    assert editor(missing_binding)["WFCalendarDescriptor"] == "生活"
    missing_life = FixtureRunner(workflow, events=[dict(base, calendar_category="invalid")], calendar_bindings={"生活": None}).run()
    assert len(missing_life.calendar) == 1 and not missing_life.alerts
    assert editor(missing_life)["WFCalendarDescriptor"] == "学习"
    assert missing_life.calendar[0]["ShowWhenRun"] is True
    # Names/IDs come only from supplied XML. Title binding in iOS is not emulated.
    different_name = FixtureRunner(workflow, events=[dict(base, calendar_category="课程安排")],
                                  calendar_bindings={"学习": {"Title": "课程安排", "Identifier": "fixture-course", "IsAllCalendar": False}}).run()
    assert editor(different_name)["WFCalendarDescriptor"] == "课程安排"
    assert different_name.variables["目标日历标识"] == "fixture-course"
    report.append("Unselected calendars fall back to the first actual selection (including when Life is absent); arbitrary stored names/IDs are read from explicit fixture XML; native title resolution remains separate from successful name/ID extraction")

    arbitrary_names = ("工作", "运动", "旅行", "家庭", "阅读", "志愿服务", "音乐", "项目.2026", "  A & <专题> \"双引号\" '单引号' 😀 &lt;  ")
    arbitrary_bindings = [{"IsAllCalendar": False, "Title": name, "Identifier": f"fixture-only-custom-{i}"}
                          for i, name in enumerate(arbitrary_names)]
    many = FixtureRunner(workflow, events=[dict(base, calendar_category=name) for name in arbitrary_names], selected_calendars=arbitrary_bindings).run()
    assert len(many.requests) == 1 and len(many.calendar) == len(arbitrary_names)
    assert [editor(many, i)["WFCalendarDescriptor"] for i in range(len(arbitrary_names))] == list(arbitrary_names)
    assert list(many.variables["可选日历名称表"].values()) == list(arbitrary_names)
    assert many.variables["默认日历名称"] == "工作"
    for name, identifier in zip(arbitrary_names, [b["Identifier"] for b in arbitrary_bindings]):
        encoded = base64.b64encode(name.encode()).decode()
        assert many.variables["已选日历标识表"][encoded] == identifier
    body = repr(many.requests[0])
    assert "fixture-key-not-real" not in body and "WFWorkflowActions" not in body and "fixture-only-custom-" not in body
    assert many.source_exports == [FILE_STEM] and not many.calendar_queries and not many.questions and not many.alerts
    many_context = many.requests[0]["messages"][1]["content"][0]["text"]
    catalog_json = many_context.split("可选日历名称JSON（值为准确名称，键仅为内部编码）：", 1)[1].split("\n无法分类时的默认日历：", 1)[0]
    assert list(json.loads(catalog_json).values()) == list(arbitrary_names)
    first_fallback = FixtureRunner(workflow, events=[dict(base, calendar_category="不存在")], selected_calendars=list(reversed(arbitrary_bindings))).run()
    assert editor(first_fallback)["WFCalendarDescriptor"] == arbitrary_names[-1]
    report.append("Nine arbitrary selected names (including dots, quotes, ampersands, emoji and leading/trailing spaces) route independently; encoded lookup keys avoid dotted-path ambiguity, XML/JSON escapes survive, first-selection order controls fallback, and only names—not source XML, API keys or calendar IDs—enter the one model request")

    only_one = FixtureRunner(workflow, events=[dict(base, calendar_category="生活")], selected_calendars=[arbitrary_bindings[0]]).run()
    assert editor(only_one)["WFCalendarDescriptor"] == "工作"
    repeated_selection = FixtureRunner(workflow, events=[base], selected_calendars=[{"IsAllCalendar": True}, arbitrary_bindings[0], arbitrary_bindings[0], {"IsAllCalendar": True}]).run()
    assert list(repeated_selection.variables["可选日历名称表"].values()) == ["工作"]
    assert editor(repeated_selection)["WFCalendarDescriptor"] == "工作" and not repeated_selection.calendar_queries
    same_name = FixtureRunner(workflow, events=[base], selected_calendars=[arbitrary_bindings[0], dict(arbitrary_bindings[0], Identifier="fixture-only-other-account")]).run()
    assert "标识不同" in same_name.alerts[-1] and not same_name.requests and not same_name.calendar
    for selection in ([], [{"IsAllCalendar": True}], [{"IsAllCalendar": False, "Title": "生活"}], [{"IsAllCalendar": False, "Identifier": "fixture-id"}]):
        unconfigured = FixtureRunner(workflow, events=[base], selected_calendars=selection).run()
        assert unconfigured.alerts and not unconfigured.requests and not unconfigured.calendar
    report.append("One or many stored selections work without fixed categories; all-calendar range placeholders are skipped, repeated name/ID bindings deduplicate, distinct IDs with one name stop before the API, and empty/incomplete configuration never fabricates a calendar")

    ready = FixtureRunner(workflow, events=[base])
    own_fixture = ready.shortcut_library[-1]
    branded_named = FixtureRunner(workflow, events=[base], shortcut_library=[dict(own_fixture, fixture_name=NAME)]).run()
    assert editor(branded_named)["WFCalendarDescriptor"] == "生活" and branded_named.source_exports == [NAME]
    for library in ([], [dict(own_fixture, fixture_name="calshot 2")], [own_fixture, dict(own_fixture, fixture_name=NAME)],
                    [dict(own_fixture, fixture_xml="AEA1")], [dict(own_fixture, fixture_xml=own_fixture["fixture_xml"].replace(NAME + " " + __version__, NAME + " 0.0.0"))]):
        wrong_source = FixtureRunner(workflow, events=[base], shortcut_library=library).run()
        assert wrong_source.alerts and not wrong_source.requests and not wrong_source.calendar
    screenshot_position = next(i for i, a in enumerate(workflow["WFWorkflowActions"]) if a["WFWorkflowActionIdentifier"].endswith("takescreenshot"))
    source_position = next(i for i, a in enumerate(workflow["WFWorkflowActions"]) if a["WFWorkflowActionIdentifier"].endswith("getmyworkflows"))
    assert screenshot_position < source_position, "Capture the original screen before library/export permissions can show UI"
    active_carrier = copy.deepcopy(workflow)
    next(a["WFWorkflowActionParameters"] for a in active_carrier["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"].endswith(".number"))["WFNumberActionNumber"] = "1"
    try:
        FixtureRunner(active_carrier, events=[base]).run()
    except AssertionError as error:
        assert "disabled calendar carrier" in str(error)
    else:
        raise AssertionError("Production executed its saved calendar selection carrier")
    report.append("Production exports only a uniquely named current CalShot, accepts its branded name and lowercase import filename, rejects missing/ambiguous/old/non-XML sources, captures before permission-sensitive lookup, and keeps the native selection carrier permanently disabled")

    malformed_calendar = copy.deepcopy(workflow)
    native = next(a["WFWorkflowActionParameters"] for a in malformed_calendar["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"].endswith("addnewevent"))
    native["WFCalendarDescriptor"] = {"WFSerializationType": "WFTextTokenString", "Value": {"string": "生活", "attachmentsByRange": {}}}
    try:
        structure(malformed_calendar)
    except AssertionError as error:
        assert "attachment" in str(error)
    else:
        raise AssertionError("A token string calendar field was accepted as the native variable fallback")
    assert not any(isinstance(value, dict) and value.get("Type") == "Ask" for value in walk(workflow))
    report.append("Dynamic calendar fields use the native string-coerced attachment shared by all reminders; token-string calendar envelopes are rejected, and production serializes no runtime Ask pickers or fixed-category import questions")

    for value, expected in ((None, "Custom"), (30, "Custom"),
                            (60, "Custom"), (0, "At time of event"), (-1, "None"),
                            (5, "Custom"), (15, "Custom"),
                            (120, "Custom"), (1440, "Custom")):
        run = FixtureRunner(workflow, events=[dict(base, reminder_minutes=value)]).run()
        event = editor(run)
        assert event["WFAlertTime"] == expected, (value, event)
        if expected == "Custom":
            assert run.date(event["WFAlertCustomTime"]) == run.date(event["WFCalendarItemStartDate"]) - dt.timedelta(minutes=30 if value is None else value)
        else:
            assert "WFAlertCustomTime" not in event
    for value in ("", " \n", "提前半小时", -2, 1.5, True, False, {}, []):
        event = editor(FixtureRunner(workflow, events=[dict(base, reminder_minutes=value)]).run())
        assert event["WFAlertTime"] == "Custom", value
        assert event["WFAlertCustomTime"] == "2026-10-02T13:30:00+08:00", value
    mixed_reminders = FixtureRunner(workflow, events=[dict(base, reminder_minutes=value) for value in (-1, 7, None, 0)]).run()
    assert [event["WFAlertTime"] for event in mixed_reminders.calendar] == ["None", "Custom", "Custom", "At time of event"]
    assert [bool(event.get("WFAlertCustomTime")) for event in mixed_reminders.calendar] == [False, True, True, False]
    report.append("Missing/invalid reminders default to 30 minutes; explicit hour, start-time zero and disabled minus-one retain distinct native alarm options without leaking between events")

    for minutes in (7, 45, 90):
        run = FixtureRunner(workflow, events=[dict(base, reminder_minutes=minutes)]).run()
        event = editor(run)
        assert event["WFAlertTime"] == "Custom"
        assert run.date(event["WFAlertCustomTime"]) == run.date(base["start"]) - dt.timedelta(minutes=minutes)
    bad_quantity = copy.deepcopy(workflow)
    for a in bad_quantity["WFWorkflowActions"]:
        p = a["WFWorkflowActionParameters"]
        if p.get("WFAdjustOperation") == "Subtract":
            magnitude = p["WFDuration"]["Value"]["Magnitude"]
            p["WFDuration"]["Value"]["Magnitude"] = {"WFSerializationType": "WFTextTokenAttachment", "Value": magnitude}
            break
    try:
        structure(bad_quantity)
    except AssertionError:
        pass
    else:
        raise AssertionError("Wrapped native quantity magnitude was accepted")
    day = editor(FixtureRunner(workflow, events=[dict(base, start="10-13", end="", all_day=True)]).run())
    assert day["WFCalendarItemStartDate"] == "2026-10-13T00:00:00+08:00"
    assert day["WFAlertTime"] == "Custom"
    assert day["WFAlertCustomTime"] == "2026-10-12T23:30:00+08:00"
    report.append("Every positive offset uses a literal Custom enum with an explicit alarm date; all-day default is 23:30 before the midnight start")

    for category in SAMPLE_CALENDARS:
        every_branch = FixtureRunner(workflow, events=[dict(base, calendar_category=category, reminder_minutes=minutes)
                                                     for minutes in (None, 0, -1)]).run()
        assert len(every_branch.calendar) == 3 and len(every_branch.requests) == 1
        for event in every_branch.calendar:
            assert event["WFCalendarDescriptor"] == category
    for kind in ("Variable", "ActionOutput"):
        old_enum = copy.deepcopy(workflow)
        native = next(a["WFWorkflowActionParameters"] for a in old_enum["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"].endswith("addnewevent"))
        reference = {"Type": kind, "VariableName": "提醒选项"}
        if kind == "ActionOutput":
            producer = next(a["WFWorkflowActionParameters"] for a in old_enum["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"].endswith("gettext"))
            reference = {"Type": kind, "OutputUUID": producer["UUID"], "OutputName": producer["CustomOutputName"],
                         "Aggrandizements": [{"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFStringContentItem"}]}
        native["WFAlertTime"] = {"WFSerializationType": "WFTextTokenAttachment", "Value": reference}
        try:
            structure(old_enum)
        except AssertionError:
            pass
        else:
            raise AssertionError("Runtime alert enum was accepted")
    for broken in (30, {"WFSerializationType": "WFTextTokenString", "Value": {"string": "30", "attachmentsByRange": {}}}):
        old_number = copy.deepcopy(workflow)
        native = next(a["WFWorkflowActionParameters"] for a in old_number["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"].endswith(".number"))
        native["WFNumberActionNumber"] = broken
        try:
            structure(old_number)
        except AssertionError:
            pass
        else:
            raise AssertionError("Invalid native number envelope was accepted")
    report.append("Every sample name uses the same dynamic calendar field for custom/start/off reminders and opens once per event; structural checks reject variable reminder enums and non-native number field envelopes")

    for shared_input, mode, source_app, suffix in (
        (None, "屏幕截图", "微信", "（根据画面识别）"),
        ({"fixture_image": "photo"}, "共享图片", "微信", "（根据画面识别）"),
        ("来源：微信\n10月13号下午6:00和朋友吃饭", "共享文字", "微信", "（原文标注）"),
    ):
        run = FixtureRunner(workflow, shared=shared_input, events=[food], source_app=source_app).run()
        notes = editor(run)["WFCalendarItemNotes"]
        assert notes.startswith("添加来源：CalShot · " + mode + " · 微信" + suffix)
        assert "\n添加时间：2026-10-01 13:17:00 +08:00\n\n原文：\n" in notes
        assert (shared_input if isinstance(shared_input, str) else food["source_text"]) in notes
    for source_app in ("", None, " \n "):
        notes = editor(FixtureRunner(workflow, events=[food], source_app=source_app).run())["WFCalendarItemNotes"]
        assert notes.startswith("添加来源：CalShot · 屏幕截图\n添加时间：")
        assert "根据画面识别" not in notes and "原文标注" not in notes
    report.append("Screenshot/shared-image/shared-text provenance is explicit; app names are labeled as image inference or text attribution and omitted when unknown")

    original = '来源：微信\n完整原文包含 "引号"\n预填说明：这是用户原文，不能删除。\n10月13日下午6点吃饭。'
    event = editor(FixtureRunner(workflow, shared=original, events=[dict(food, source_text="模型摘要", notes="请携带证件")]).run())
    assert "原文：\n" + original in event["WFCalendarItemNotes"]
    assert "模型摘要" not in event["WFCalendarItemNotes"]
    assert event["WFCalendarItemNotes"].endswith("活动备注：\n请携带证件")
    empty_original = editor(FixtureRunner(workflow, events=[dict(base, source_text=None, notes=" \n ")]).run())
    assert empty_original["WFCalendarItemNotes"].endswith("原文：\n（未提取到原文）")
    assert "活动备注" not in empty_original["WFCalendarItemNotes"]
    report.append("Shared text retains the full original including whitespace and literal diagnostic words; images without original text are labeled, and actual activity notes remain optional")

    delayed = FixtureRunner(workflow, events=[dict(base, calendar_category=name) for name in ("生活", "科研", "作业")], editor_delay=120).run()
    for i, stamp in enumerate(("13:17:00", "13:19:00", "13:21:00")):
        event = editor(delayed, i)
        notes = event["WFCalendarItemNotes"]
        assert "添加时间：2026-10-01 " + stamp + " +08:00" in notes
        assert notes.index("添加来源：") < notes.index("添加时间：") < notes.index("原文：") < notes.index("活动备注：")
        assert event["WFCalendarItemStartDate"] == base["start"]
    assert len(delayed.requests) == 1
    report.append("Each editor receives its own generation timestamp after simulated user delays; event times remain unchanged and metadata order is stable")

    custom_alarm_action = next(a["WFWorkflowActionParameters"]["UUID"] for a in workflow["WFWorkflowActions"]
                               if a["WFWorkflowActionIdentifier"].endswith("adjustdate")
                               and a["WFWorkflowActionParameters"].get("WFAdjustOperation") == "Subtract")
    failed_alarm = FixtureRunner(workflow, events=[dict(base, reminder_minutes=7)], empty_outputs=[custom_alarm_action]).run()
    assert not failed_alarm.calendar and "自定义提醒" in failed_alarm.alerts[-1]
    stamp_action = next(a["WFWorkflowActionParameters"]["UUID"] for a in workflow["WFWorkflowActions"]
                        if a["WFWorkflowActionIdentifier"].endswith("format.date")
                        and a["WFWorkflowActionParameters"].get("WFDateFormat") == "yyyy-MM-dd HH:mm:ss XXX")
    failed_stamp = FixtureRunner(workflow, events=[base], empty_outputs=[stamp_action]).run()
    assert not failed_stamp.calendar and "添加时间" in failed_stamp.alerts[-1]
    report.append("Native custom-alarm or provenance-time failures report necessary errors before compose; successful paths contain no intermediate questions or alerts")

    # Building another workflow must not mutate the production workflow snapshot.
    diagnostic = build_editor_check()
    assert workflow == build(calendar_slots)
    structure(diagnostic, require_key=False)
    assert not any(a["WFWorkflowActionIdentifier"].endswith(("downloadurl", "takescreenshot", "ask"))
                   for a in diagnostic["WFWorkflowActions"])
    check_run = FixtureRunner(diagnostic).run()
    assert not check_run.requests and not check_run.questions and len(check_run.calendar) == 1
    assert "1800" in check_run.alerts[0]
    assert check_run.calendar[0]["WFCalendarItemStartDate"] == "2026-10-08T14:00:00+08:00"
    assert check_run.calendar[0]["WFCalendarItemEndDate"] == "2026-10-08T14:30:00+08:00"
    reversed_diagnostic = copy.deepcopy(diagnostic)
    for a in reversed_diagnostic["WFWorkflowActions"]:
        p = a["WFWorkflowActionParameters"]
        if a["WFWorkflowActionIdentifier"].endswith("gettimebetweendates"):
            p["WFInput"], p["WFTimeUntilFromDate"] = p["WFTimeUntilFromDate"], p["WFInput"]
    failed_check = FixtureRunner(reversed_diagnostic).run()
    assert len(failed_check.calendar) == 1 and "-1800" in failed_check.alerts[0]
    assert failed_check.calendar[0]["WFCalendarItemStartDate"] == "2026-10-08T14:00:00+08:00"
    assert plistlib.loads(plistlib.dumps(diagnostic)) == diagnostic

    reminder_probe = build_reminder_check()
    structure(reminder_probe, require_key=False)
    probe_run = FixtureRunner(reminder_probe).run()
    assert len(probe_run.calendar) == 1 and not probe_run.requests and not probe_run.questions and not probe_run.alerts
    probe_event = probe_run.calendar[0]
    assert probe_event["WFAlertTime"] == "30 minutes before" and "WFAlertCustomTime" not in probe_event
    assert probe_event["WFCalendarItemStartDate"] == "2026-10-13T18:00:00+08:00"
    assert probe_event["WFCalendarItemEndDate"] == "2026-10-13T19:00:00+08:00"
    assert probe_event["ShowWhenRun"] is True and probe_event["WFCalendarItemShowComposeSheet"] is True
    bad_probe = copy.deepcopy(reminder_probe)
    bad_probe["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]["WFAlertTime"] = {"WFSerializationType": "WFTextTokenAttachment", "Value": {"Type": "Variable", "VariableName": "提醒选项"}}
    try:
        structure(bad_probe, require_key=False)
    except AssertionError:
        pass
    else:
        raise AssertionError("Variable-based alert was accepted in the literal alarm probe")
    write_reminder_check()
    assert plistlib.loads(plistlib.dumps(reminder_probe)) == reminder_probe
    report.append("API-free alarm probe opens one editor with a literal 30-minute preset and no custom-time field, setup questions or runtime prompts; user screenshot separately confirms this preset in the second iOS26.6 alarm row, without save or notification-delivery evidence")
    custom_probe = build_reminder_check(custom=True)
    structure(custom_probe, require_key=False)
    custom_run = FixtureRunner(custom_probe).run()
    assert len(custom_run.calendar) == 1 and not custom_run.requests and not custom_run.questions and not custom_run.alerts
    custom_event = custom_run.calendar[0]
    assert custom_event["WFAlertTime"] == "Custom"
    assert custom_event["WFCalendarItemStartDate"] == probe_event["WFCalendarItemStartDate"]
    assert custom_event["WFCalendarItemEndDate"] == probe_event["WFCalendarItemEndDate"]
    start = custom_run.date(custom_event["WFCalendarItemStartDate"])
    alarm = custom_run.date(custom_event["WFAlertCustomTime"])
    assert (start - alarm).total_seconds() == 1800
    assert custom_event["ShowWhenRun"] is True and custom_event["WFCalendarItemShowComposeSheet"] is True
    assert custom_probe["WFWorkflowName"] != reminder_probe["WFWorkflowName"]
    assert plistlib.loads(plistlib.dumps(custom_probe)) == custom_probe
    assert workflow == build(calendar_slots), "Reminder diagnostics must not mutate the production workflow"
    report.append("A separately named API-free custom-alarm comparison opens the same fixed event with a literal Custom enum and 17:30 text date, exactly 30 minutes before start; user screenshot separately confirms that time in the second iOS26.6 alarm row, while production-variable and save behavior remain unverified")
    runtime_probe = build_reminder_runtime_check()
    structure(runtime_probe, require_key=False, runtime_reminder=True)
    runtime_run = FixtureRunner(runtime_probe).run()
    assert len(runtime_run.calendar) == 1 and not runtime_run.requests and not runtime_run.questions and not runtime_run.alerts
    runtime_event = runtime_run.calendar[0]
    assert runtime_event["WFAlertTime"] == "Custom"
    assert runtime_event["WFCalendarItemStartDate"] == probe_event["WFCalendarItemStartDate"]
    assert runtime_event["WFCalendarItemEndDate"] == probe_event["WFCalendarItemEndDate"]
    assert "计算提醒分钟：30" in runtime_event["WFCalendarItemNotes"]
    assert "提醒选项：Custom" in runtime_event["WFCalendarItemNotes"]
    assert runtime_event["WFAlertCustomTime"] == "2026-10-13T17:30:00+08:00"
    assert runtime_probe["WFWorkflowName"] not in (reminder_probe["WFWorkflowName"], custom_probe["WFWorkflowName"])
    for minutes, option, alarm in ((-1, "None", None), (0, "At time of event", None),
                                   (60, "Custom", "2026-10-13T17:00:00+08:00"), (7, "Custom", "2026-10-13T17:53:00+08:00")):
        example = copy.deepcopy(runtime_probe)
        fixture = next(a["WFWorkflowActionParameters"] for a in example["WFWorkflowActions"]
                       if a["WFWorkflowActionParameters"].get("CustomOutputName") == "固定提醒响应")
        fixture["WFTextActionText"]["Value"]["string"] = json.dumps({"events": [{"reminder_minutes": minutes}]})
        example_run = FixtureRunner(example).run()
        assert not example_run.requests and not example_run.questions and not example_run.alerts
        assert example_run.calendar[0]["WFAlertTime"] == option
        assert example_run.calendar[0].get("WFAlertCustomTime") == alarm
    assert plistlib.loads((ROOT / "reminder-runtime-check/calshot-reminder-runtime-check.unsigned.shortcut").read_bytes()) == runtime_probe
    assert workflow == build(calendar_slots)
    report.append("API-free runtime probe reuses production literal alarm branches and calculated custom date; null becomes 17:30 for an 18:00 start, off/start/hour/custom cases stay separate; phone result remains pending")
    report.append("No-API diagnostic reports each date stage, then independently opens compose with literal October 8 14:00-14:30; reversed interval does not change its fixed editor inputs")

    old_date_input = copy.deepcopy(workflow)
    for a in old_date_input["WFWorkflowActions"]:
        p = a["WFWorkflowActionParameters"]
        if a["WFWorkflowActionIdentifier"].endswith("format.date"):
            ref, = p["WFDate"]["Value"]["attachmentsByRange"].values()
            p["WFDate"] = {"WFSerializationType": "WFTextTokenAttachment", "Value": ref}
    try:
        structure(old_date_input)
    except AssertionError as error:
        assert "token string" in str(error)
    else:
        raise AssertionError("Old disconnected Format Date input was accepted")
    broken_format = FixtureRunner(old_date_input, events=[base]).run()
    assert not broken_format.calendar and "系统未能格式化" in broken_format.alerts[-1]
    bad_output_name = copy.deepcopy(workflow)
    for value in walk(bad_output_name):
        if isinstance(value, dict) and value.get("Type") == "ActionOutput":
            value["OutputName"] = "UNBOUND_RESULT"
            break
    try:
        structure(bad_output_name)
    except AssertionError as error:
        assert "reference name" in str(error)
    else:
        raise AssertionError("Unbound output name was accepted")
    report.append("Video regression rejects bare Format Date inputs and unbound output names; disconnected dates stop with a precise formatting error")

    for name, expected_message in (("date", "系统未能格式化设备年份"),
                                   ("format.date", "系统未能格式化"),
                                   ("gettimebetweendates", "系统未返回起止时间间隔")):
        empty_production = FixtureRunner(workflow, events=[base], empty_outputs=[name]).run()
        assert not empty_production.calendar and expected_message in empty_production.alerts[-1]
        empty_diagnostic = FixtureRunner(diagnostic, empty_outputs=[name]).run()
        assert len(empty_diagnostic.calendar) == 1
        assert "空值" in empty_diagnostic.alerts[0] or "无法计算" in empty_diagnostic.alerts[0]
        assert empty_diagnostic.calendar[0]["WFCalendarItemStartDate"] == "2026-10-08T14:00:00+08:00"
        assert empty_diagnostic.calendar[0]["WFCalendarItemEndDate"] == "2026-10-08T14:30:00+08:00"
    report.append("Absent native date, formatted date or interval stops production before saving; diagnostic still tests the editor with explicit fixed inputs")

    specified_start = next(a["WFWorkflowActionParameters"]["UUID"] for a in workflow["WFWorkflowActions"]
                           if a["WFWorkflowActionIdentifier"].endswith(".date")
                           and a["WFWorkflowActionParameters"].get("WFDateActionMode") == "Specified Date")
    unreadable_native = FixtureRunner(workflow, events=[base], empty_outputs=[specified_start]).run()
    event = editor(unreadable_native)
    assert event["WFCalendarItemStartDate"] == "2026-10-01T13:17:00+08:00"
    assert "未被系统日期操作读取" not in event["WFCalendarItemNotes"]
    assert base["source_text"] in event["WFCalendarItemNotes"]
    report.append("An empty parsed model date uses the existing date fallback; genuine missing device formatting or interval outputs still report technical errors")

    for reason in ("length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted", "unexpected", "", None):
        api = {"choices": [{"finish_reason": reason, "message": {"content": json.dumps({"events": [base]})}}]}
        incomplete = FixtureRunner(workflow, api=api).run()
        assert not incomplete.calendar and not incomplete.questions and incomplete.alerts, reason
    missing_reason = FixtureRunner(workflow, api={"choices": [{"message": {"content": json.dumps({"events": [base]})}}]}).run()
    assert not missing_reason.calendar and missing_reason.alerts
    report.append("Only finish_reason=stop continues; incomplete, missing and unknown statuses stop before any calendar editor")

    empty = FixtureRunner(workflow).run()
    assert not empty.calendar and empty.alerts
    truncated = FixtureRunner(workflow, api={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}).run()
    assert not truncated.calendar
    error = FixtureRunner(workflow, api={"error": {"message": "fixture auth error"}}).run()
    assert not error.calendar and "fixture auth error" in error.alerts[-1]
    report.append("No events, truncated output, and API error fixtures never reach the calendar action")

    selection_probe = build_calendar_selection_check(import_setup=False)
    structure(selection_probe, require_key=False, calendar_selection_probe=True)
    assert plistlib.loads(plistlib.dumps(selection_probe)) == selection_probe
    actions = selection_probe["WFWorkflowActions"]
    assert not any(a["WFWorkflowActionIdentifier"].endswith(("addnewevent", "removeevents", "downloadurl", "savefile", "setclipboard")) for a in actions)
    assert PLACEHOLDER not in repr(selection_probe)
    carrier_index = next(i for i, a in enumerate(actions) if a["WFWorkflowActionParameters"].get("CustomOutputName") == "不执行的日历参数载体")
    carrier = actions[carrier_index]["WFWorkflowActionParameters"]["WFGetUpcomingItemCalendar"]
    assert carrier == {"IsAllCalendar": True}
    assert "Identifier" not in carrier and "Title" not in carrier
    assert "验证2" in actions[0]["WFWorkflowActionParameters"]["WFCommentActionText"]

    def selection_run(descriptor=None, xml_override=None, present=True, duplicate=False, own_name=CALENDAR_SELECTION_CHECK_NAME):
        # Explicit fixture XML replaces the unknown native export result. There
        # is no simulated calendar enumeration or event-store access here.
        configured = copy.deepcopy(selection_probe)
        parameters = configured["WFWorkflowActions"][carrier_index]["WFWorkflowActionParameters"]
        if descriptor is not None:
            parameters["WFGetUpcomingItemCalendar"] = descriptor
        if duplicate:
            configured["WFWorkflowActions"].append(copy.deepcopy(configured["WFWorkflowActions"][carrier_index]))
        xml = plistlib.dumps(configured, fmt=plistlib.FMT_XML, sort_keys=False).decode()
        library = [{"fixture_name": "其他快捷指令", "fixture_xml": "MUST NOT BE EXPORTED"}]
        if present:
            library.append({"fixture_name": own_name,
                            "fixture_xml": xml if xml_override is None else xml_override})
        return FixtureRunner(selection_probe, shortcut_library=library).run()

    for title, identifier in (("生活", "fixture-only-a"), ("科研", "fixture-only-b"),
                              ("空日历", "fixture-only-empty"),
                              (" A & <专题> \"双引号\" '单引号' &lt; ", "fixture-only-xml"),
                              ("同名日历", "fixture-only-account-1"), ("同名日历", "fixture-only-account-2")):
        selected = selection_run({"Identifier": identifier, "IsAllCalendar": False, "Title": title})
        assert selected.variables["选中日历名称"] == title, selected.alerts
        assert selected.variables["选中日历标识"] == identifier
        assert title in selected.shortcut_output and identifier in selected.shortcut_output
        assert len(selected.alerts) == 1 and selected.source_exports == [CALENDAR_SELECTION_CHECK_NAME]
        assert not selected.requests and not selected.calendar and not selected.questions and not selected.calendar_queries
    no_id = selection_run({"Title": "生活", "IsAllCalendar": False})
    assert no_id.variables["选中日历标识"] == "" and "仅名称提取成功" in no_id.shortcut_output
    file_named = selection_run({"IsAllCalendar": False, "Title": "生活", "Identifier": "fixture-only-filename"}, own_name=CALENDAR_SELECTION_CHECK_STEM)
    assert file_named.variables["选中日历名称"] == "生活" and file_named.source_exports == [CALENDAR_SELECTION_CHECK_STEM]
    report.append("Calendar selection probe extracts only its own configured descriptor into name/ID variables, with an explicit mocked XML export; changed/empty-labelled calendars, same-name distinct IDs and XML escapes are preserved, missing IDs stay empty; this does not verify native XML export or multi-selection")
    for selected, expected in ((selection_run({"IsAllCalendar": False}), "没有得到日历名称"),
                               (selection_run(present=False), "没有找到名为"),
                               (selection_run(own_name="其他名称"), "没有找到名为"),
                               (selection_run(xml_override="AEA1"), "没有得到可读取的XML"),
                               (selection_run(), "只有范围标记"),
                               (selection_run({"IsAllCalendar": False, "Title": "生活"}, duplicate=True), "没有找到唯一")):
        assert selected.shortcut_output is None and expected in selected.alerts[-1]
        assert not selected.calendar and not selected.requests and not selected.calendar_queries
    report.append("Calendar selection probe rejects unconfigured/renamed/non-XML/ambiguous input without fabricated names or event-derived fallbacks; no API, calendar mutations or persistent file writes are serialized")
    for supplied_events in ([], ["fixture-only-event"]):
        runtime_picker = FixtureRunner(selection_probe, selection_mode=1, upcoming_fixture=supplied_events).run()
        assert len(runtime_picker.calendar_queries) == 1 and len(runtime_picker.alerts) == 1
        assert f"日程数：{len(supplied_events)}" in runtime_picker.shortcut_output
        assert "没有读取选中名称" in runtime_picker.shortcut_output
        assert not runtime_picker.source_exports and not runtime_picker.requests and not runtime_picker.calendar
        assert "选中日历名称" not in runtime_picker.variables, "A transient Ask selection cannot be inferred from events or the stored descriptor"
    cancelled = FixtureRunner(selection_probe, selection_mode=1, upcoming_fixture=[], cancel_picker=True).run()
    assert len(cancelled.calendar_queries) == 1 and not cancelled.alerts and cancelled.shortcut_output is None
    report.append("Selection verification2 has no blank import question; its menu keeps literal Ask picker/query (Today, at most one event) separate from persisted binding extraction, permits an explicitly mocked empty result and cancellation, recognizes the imported English filename, and never reports a transient name as persisted; native picker visibility still requires iOS 26.6 verification")
    enabled_carrier = copy.deepcopy(selection_probe)
    enabled_carrier["WFWorkflowActions"][1]["WFWorkflowActionParameters"]["WFNumberActionNumber"] = "1"
    try:
        FixtureRunner(enabled_carrier, shortcut_library=[]).run()
    except AssertionError as error:
        assert "disabled calendar carrier" in str(error)
    else:
        raise AssertionError("Selection carrier was not protected by the false branch")

    import_probe = build_calendar_selection_check()
    structure(import_probe, require_key=False, calendar_import_probe=True)
    assert plistlib.loads(plistlib.dumps(import_probe)) == import_probe
    question, = import_probe["WFWorkflowImportQuestions"]
    assert "验证3" in import_probe["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["WFCommentActionText"]
    assert PLACEHOLDER not in repr(import_probe)
    assert not any(a["WFWorkflowActionIdentifier"].endswith(("addnewevent", "removeevents", "downloadurl", "savefile", "setclipboard", "ask")) for a in import_probe["WFWorkflowActions"])

    def import_selection_run(descriptor):
        # This fixture supplies exported XML after a hypothetical installation;
        # it does NOT simulate the native import UI or prove it saves a choice.
        configured = copy.deepcopy(import_probe)
        configured["WFWorkflowActions"][question["ActionIndex"]]["WFWorkflowActionParameters"][question["ParameterKey"]] = descriptor
        xml = plistlib.dumps(configured, fmt=plistlib.FMT_XML, sort_keys=False).decode()
        return FixtureRunner(import_probe, shortcut_library=[{"fixture_name": CALENDAR_SELECTION_CHECK_STEM, "fixture_xml": xml}]).run()

    for title, identifier in (("生活", "fixture-install-life"), ("空日历", "fixture-install-empty"),
                              ("日历 & <专题> \"引号\" &lt;", "fixture-install-escaped")):
        supplied = {"Title": title, "Identifier": identifier, "IsAllCalendar": False}
        selected = import_selection_run(supplied)
        repeated = import_selection_run(supplied)
        assert selected.variables["选中日历名称"] == title and selected.variables["选中日历标识"] == identifier
        assert selected.shortcut_output == repeated.shortcut_output and "安装选择读取结果" in selected.shortcut_output
        assert not selected.requests and not selected.calendar_queries and not selected.calendar and not selected.questions
        assert selected.source_exports == [CALENDAR_SELECTION_CHECK_STEM]
    report.append("Install-selection verification3 uses one native import question with a complete All Calendars default and reads only its saved name/ID; supplied XML covers an empty calendar and escaped titles without editing, event queries, API or fabricated IDs; the user's video separately confirms one installed Research name/ID, while empty-calendar/multiple-choice cases remain unverified")
    for supplied, expected in (({"IsAllCalendar": True}, "未读到具体选择"),
                               ({"IsAllCalendar": False}, "没有得到日历名称")):
        missing = import_selection_run(supplied)
        assert missing.shortcut_output is None and expected in missing.alerts[-1]
        assert "请在0等于1" not in missing.alerts[-1] and "请点本快捷指令的…编辑" not in missing.alerts[-1]
    enabled = copy.deepcopy(import_probe)
    enabled["WFWorkflowActions"][1]["WFWorkflowActionParameters"]["WFNumberActionNumber"] = "1"
    try:
        FixtureRunner(enabled, shortcut_library=[]).run()
    except AssertionError as error:
        assert "disabled calendar carrier" in str(error)
    else:
        raise AssertionError("Install-selection carrier was not protected by the false branch")
    report.append("Install-selection verification3 reports an unchanged range or missing saved title honestly instead of instructing manual editing; the disabled carrier cannot run, and this test does not claim first-run setup or multiple-calendar selection")
    assert workflow == build(calendar_slots), "Experimental selection probe must not modify the production workflow"

    performance_probe = build_performance_check()
    structure(performance_probe, require_key=False)
    names = [a["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.") for a in performance_probe["WFWorkflowActions"]]
    assert not set(names) & {"downloadurl", "takescreenshot", "addnewevent", "getupcomingevents", "savefile", "ask"}
    assert not performance_probe["WFWorkflowTypes"] and not performance_probe["WFWorkflowInputContentItemClasses"]
    delay_fixture = {"getmyworkflows": 3, "getitemname": 1, "gettypeaction": 4,
                     "setitemname": 2, "正式快捷指令配置XML": 5}
    timed = FixtureRunner(performance_probe, shortcut_library=ready.shortcut_library, action_delays=delay_fixture).run()
    assert len(timed.alerts) == 1 and not timed.requests and not timed.calendar and not timed.questions and not timed.calendar_queries
    message = timed.alerts[0]
    for label, seconds in (("获取快捷指令列表", 3), ("匹配正式名称", 2),
                           ("导出并读取配置", 11), ("解析日历选择", 0), ("总计", 16)):
        assert float(re.search(re.escape(label) + r"：([0-9.]+) 秒", message)[1]) == seconds
    assert "快捷指令数量：2" in message and "日历配置项数量（含未用项）：5" in message
    assert all(secret not in message for secret in ("fixture-key-not-real", "WFWorkflowActions", "fixture-calendar-"))
    assert timed.source_exports == [FILE_STEM]
    report.append("Configuration performance diagnostic reuses production lookup/export/parse, attributes explicit fixture delays to four phases and total, accepts a genuine zero duration, reports only counts/times, and performs no API, screenshot, event query/write or setting prompt")

    for count in (20, 100):
        library = [{"fixture_name": f"其他{i}", "fixture_xml": "DO NOT EXPORT"} for i in range(count - 1)] + [own_fixture]
        run = FixtureRunner(performance_probe, shortcut_library=library, action_delays={"getitemname": 1}).run()
        assert len(run.alerts) == 1 and f"快捷指令数量：{count}" in run.alerts[0]
        assert float(re.search(r"匹配正式名称：([0-9.]+) 秒", run.alerts[0])[1]) == count
        assert run.source_exports == [FILE_STEM] and not run.requests and not run.calendar
    for missing_action in ("format.date", "gettimebetweendates"):
        failed = FixtureRunner(performance_probe, shortcut_library=ready.shortcut_library, empty_outputs=(missing_action,)).run()
        assert failed.alerts and "没有可靠的耗时结果" in failed.alerts[-1]
        assert "总计：" not in failed.alerts[-1]
    assert workflow == build(calendar_slots), "Timing instrumentation must not change the formal release or its signature input"
    report.append("Performance diagnostic separates explicit simulated name-scan delays for 20/100 shortcuts, exports only CalShot, stops on absent timing output rather than inventing zero, and leaves the formal workflow unchanged")

    assert not any(a["WFWorkflowActionIdentifier"] == "is.workflow.actions.notification"
                   for a in workflow["WFWorkflowActions"])
    serialized_formal = json.dumps(workflow, ensure_ascii=False)
    assert all(label not in serialized_formal for label in
               ("阶段诊断", "诊断上一阶段", "诊断上一时刻", "诊断开始时刻", "诊断阶段时刻", "诊断耗时说明"))
    report.append("Formal rollback removes every stage notification, diagnostic clock and switch; existing date, reminder, calendar, source and error regressions still run without intermediate input windows")

    # Verify the delivered plist exactly matches what was checked.
    delivered = plistlib.loads((ROOT / f"{FILE_STEM}.unsigned.shortcut").read_bytes())
    assert delivered == workflow
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256((ROOT / manifest["signInput"]).read_bytes()).hexdigest() == manifest["signInputSHA256"]
    result = {"status": "passed", "version": manifest["version"], "calendarSlots": calendar_slots, "structuralChecks": "UUIDs, bound output names, references, UTF-16 attachments, control-flow groups, typed comparisons, one API question and one native import question per calendar slot, disabled calendar selection block, dynamic name attachments shared by three compose actions, no fabricated calendar identifiers, compose flags, literal alert enums, native number fields, single date operand, raw quantity magnitude tokens, Date text-slot envelopes and custom patterns, ISO text inputs, plist round-trip and hash",
              "fixtureChecks": report, "actionCount": len(workflow["WFWorkflowActions"]),
              "signInputSHA256": manifest["signInputSHA256"],
              "scope": {"localStructureAndFixtures": "passed", "nativeIOSExecuted": False, "liveAPICalled": False,
                        "signingRecordedSeparately": "manifest.json signing field; not established by local checks"},
              "limits": "Fixture evaluation is not iOS execution. User confirmed the verification3 native install picker preserves a selected name/ID, later clarified iOS27 and asked to defer the Add Shortcut button issue. Multiple install choices, arbitrary slot counts and dynamic calendar-name binding in production, calculated reminders, date parsing/UI/save, permissions, signing and live model classification/source recognition remain separate acceptance items."}
    (ROOT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(globals().get("CALSHOT_CALENDAR_SLOTS", DEFAULT_CALENDAR_SLOTS))
