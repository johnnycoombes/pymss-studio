"""Workflow hooks for deferred Vorbis saves and the 4-stem DJ fold.

pymss rejects ``ogg`` in YAML and encodes Vorbis without a quality setting.
Save nodes therefore write float WAV, and this module encodes OGG only after
the graph has finished. Intermediate ensemble and chain audio stays in the
graph as float.
"""

from __future__ import annotations

import copy
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import Any

from stem_delivery import (
    DJ_STEM_NAMES,
    StemDeliveryError,
    dj_role,
    encode_stem_group,
    is_stock_naming_template,
    load_audio_channels,
    looks_like_ogg_quality,
    missing_dj_roles,
    resolve_ogg_quality,
)

_SAVE_NODE_TYPES = {"pymss_save_audio"}
_DJ_INPUTS = ("mix", "vocals", "bass", "drums")
_DJ_SLOT = {"vocals": 0, "melody": 1, "bass": 2, "drums": 3}


def _step_id(step: dict[str, Any]) -> str:
    return str(step.get("id") or "").strip()


def _is_ogg_format(value: Any) -> bool:
    return str(value or "").strip().lower() in {"ogg", "vorbis"}


class OggSavePlan:
    """Which save nodes must be transcoded, and at which Vorbis quality."""

    def __init__(self) -> None:
        self.node_quality: dict[str, int] = {}
        self.step_formats: dict[str, str] = {}
        self.run_wants_ogg = False

    @property
    def active(self) -> bool:
        return bool(self.node_quality) or self.run_wants_ogg

    def quality_for(self, node_id: str, fallback: int) -> int:
        return self.node_quality.get(str(node_id), fallback)


def rewrite_yaml_ogg(data: dict[str, Any], audio_params: Any, payload_format: str) -> OggSavePlan:
    """Turn OGG format fields into WAV so pymss will load the workflow.

    The original choice is remembered by step id. Filename rendering can still
    use ``.ogg``; the save widget itself must stay a float WAV until the end.
    """
    plan = OggSavePlan()
    quality = resolve_ogg_quality(audio_params)
    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        data["defaults"] = defaults
    default_format = str(defaults.get("output_format") or "").strip().lower()
    payload = str(payload_format or "").strip().lower()
    plan.run_wants_ogg = _is_ogg_format(payload) or _is_ogg_format(default_format)
    if _is_ogg_format(default_format):
        defaults["output_format"] = "wav"
    steps = data.get("steps")
    if not isinstance(steps, list):
        return plan
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = _step_id(step)
        explicit = str(step.get("output_format") or "").strip().lower()
        inherits = not explicit
        wants_ogg = _is_ogg_format(explicit) or (inherits and _is_ogg_format(default_format))
        if explicit and _is_ogg_format(explicit):
            step["output_format"] = "wav"
        if wants_ogg and step_id:
            plan.step_formats[step_id] = "ogg"
            plan.node_quality[f"step:{step_id}"] = quality
    if plan.run_wants_ogg:
        plan.node_quality["__run__"] = quality
    return plan


def _widget_list(node: Any) -> list[Any]:
    data = getattr(node, "data", None)
    if not isinstance(data, dict):
        data = {}
        node.data = data
    widgets = data.get("widgets_values")
    if not isinstance(widgets, list):
        widgets = []
        data["widgets_values"] = widgets
    return widgets


def _read_widget_quality(widgets: list[Any]) -> Any:
    if not widgets:
        return None
    last = widgets[-1]
    return last if looks_like_ogg_quality(last) else None


def force_float_wav_widgets(widgets: list[Any]) -> None:
    """Point a save node at 32-bit float WAV at 44.1 kHz without dropping a quality widget."""
    if not widgets:
        widgets.extend(["wav", "Default", "44100", "FLOAT", "PCM_24", "320k"])
    widgets[0] = "wav"
    second = str(widgets[1] if len(widgets) > 1 else "")
    if second.isdigit():
        widgets[1] = "44100"
        while len(widgets) <= 2:
            widgets.append("FLOAT")
        widgets[2] = "FLOAT"
        return
    while len(widgets) < 4:
        widgets.append("")
    widgets[2] = "44100"
    widgets[3] = "FLOAT"


def mark_compiled_ogg_saves(dag: Any, plan: OggSavePlan, audio_params: Any) -> None:
    """After YAML compilation, record save-node ids and force float WAV widgets."""
    quality = resolve_ogg_quality(audio_params)
    run_ogg = plan.run_wants_ogg or bool(plan.step_formats)
    for node in list(getattr(dag, "nodes", []) or []):
        if str(getattr(node, "type", "")) not in _SAVE_NODE_TYPES:
            continue
        node_id = str(getattr(node, "id", ""))
        step_id = ""
        if node_id.startswith("save:"):
            parts = node_id.split(":")
            if len(parts) >= 3:
                step_id = parts[1]
        ensemble_save = node_id.startswith("studio:ensemble-save:")
        wants = step_id in plan.step_formats or (ensemble_save and plan.run_wants_ogg)
        if not wants and run_ogg and _is_ogg_format(_widget_list(node)[:1] or [""]):
            wants = True
        if not wants:
            continue
        widgets = _widget_list(node)
        widget_quality = _read_widget_quality(widgets)
        plan.node_quality[node_id] = resolve_ogg_quality(audio_params, widget_quality) if widget_quality is not None else quality
        force_float_wav_widgets(widgets)
    if plan.run_wants_ogg and "__run__" not in plan.node_quality:
        plan.node_quality["__run__"] = quality


def rewrite_graph_ogg(dag: Any, audio_params: Any, payload_format: str) -> OggSavePlan:
    """Rewrite advanced-graph save nodes that selected OGG."""
    plan = OggSavePlan()
    plan.run_wants_ogg = _is_ogg_format(payload_format)
    quality = resolve_ogg_quality(audio_params)
    for node in list(getattr(dag, "nodes", []) or []):
        if str(getattr(node, "type", "")) not in _SAVE_NODE_TYPES:
            continue
        widgets = _widget_list(node)
        fmt = str(widgets[0] if widgets else "").strip().lower()
        if not _is_ogg_format(fmt):
            continue
        widget_quality = _read_widget_quality(widgets)
        plan.node_quality[str(node.id)] = (
            resolve_ogg_quality(audio_params, widget_quality) if widget_quality is not None else quality
        )
        force_float_wav_widgets(widgets)
    if plan.run_wants_ogg:
        plan.node_quality.setdefault("__run__", quality)
    return plan


def _audio_stems(node: Any) -> list[str]:
    outputs = (getattr(node, "data", None) or {}).get("outputs") if isinstance(getattr(node, "data", None), dict) else None
    stems: list[str] = []
    for output in outputs or []:
        if not isinstance(output, dict):
            continue
        name = str(output.get("name") or "")
        kind = str(output.get("type") or "")
        if name.endswith(" (Audio)"):
            stems.append(name[: -len(" (Audio)")].strip())
        elif kind.upper() == "AUDIO" and name:
            stems.append(name.strip())
    return [stem for stem in stems if stem]


def _next_link_id(dag: Any) -> int:
    highest = 0
    for node in getattr(dag, "nodes", []) or []:
        for link in getattr(node, "inputs", []) or []:
            if link is not None and isinstance(getattr(link, "link_id", None), int):
                highest = max(highest, int(link.link_id))
    return highest + 1


def _retarget(dag: Any, old_id: str, new_id: str) -> None:
    for node in dag.nodes:
        if str(node.id) == old_id:
            node.id = new_id
            node.title = new_id
        for link in node.inputs or []:
            if link is None:
                continue
            if str(link.source_node_id) == old_id:
                link.source_node_id = new_id
            if str(link.target_node_id) == old_id:
                link.target_node_id = new_id


def _role_slot(stem: str) -> int:
    role = dj_role(stem)
    if role in _DJ_SLOT:
        return _DJ_SLOT[role]
    return _DJ_SLOT["melody"]


def _dj_output_template(names: dict[str, Any], source_stem: str | None) -> str:
    template = ""
    if source_stem and isinstance(names, dict):
        raw = names.get(source_stem)
        if raw is None:
            raw = next((value for key, value in names.items() if str(key).lower() == source_stem.lower()), None)
        template = str(raw or "")
    if is_stock_naming_template(template, enabled=bool(str(template or "").strip())):
        return "%filename% - %stem%"
    return template


def apply_simple_dj_folds(dag: Any, definition: dict[str, Any]) -> dict[str, tuple[str, int]]:
    """Insert a residual fold for simple steps that opted into 4-stem DJ output.

    Returns ``step.stem`` source overrides so ensembles and downstream steps
    read the folded stems. Folded-away names (other, guitar, piano) point at
    Melody. Separator outputs stay in the graph; only links move.
    """
    import pymss.graph as graph

    steps = definition.get("steps")
    if not isinstance(steps, list):
        return {}
    overrides: dict[str, tuple[str, int]] = {}
    link_id = _next_link_id(dag)
    nodes = {str(node.id): node for node in dag.nodes}

    for step in steps:
        if not isinstance(step, dict) or step.get("dj_output") is not True:
            continue
        step_id = _step_id(step)
        sep = nodes.get(f"step:{step_id}")
        if sep is None:
            raise StemDeliveryError(f"4-stem DJ output could not find separation step {step_id or '(missing)'}.")
        stems = _audio_stems(sep)
        missing = missing_dj_roles(stems)
        if missing:
            raise StemDeliveryError(
                f"Step {step_id} cannot use 4-stem DJ output because the model stems are missing: {', '.join(missing)}."
            )
        role_index = {dj_role(stem): index for index, stem in enumerate(stems) if dj_role(stem) in {"vocals", "bass", "drums"}}
        mix_link = sep.inputs[0] if sep.inputs else None
        if mix_link is None:
            raise StemDeliveryError(f"Step {step_id} has no mix input for 4-stem DJ output.")

        fold_id = f"studio:dj:{step_id}"
        fold_inputs = []
        sources = [
            (mix_link.source_node_id, mix_link.source_slot),
            (sep.id, role_index["vocals"] * 2),
            (sep.id, role_index["bass"] * 2),
            (sep.id, role_index["drums"] * 2),
        ]
        for slot, (source_id, source_slot) in enumerate(sources):
            fold_inputs.append(graph.DAGLink(
                link_id=link_id,
                source_node_id=source_id,
                source_slot=source_slot,
                target_node_id=fold_id,
                target_slot=slot,
                type=graph.AUDIO,
            ))
            link_id += 1
        signature = graph.NodeSignature(
            inputs=[graph.PortSpec(name, graph.AUDIO) for name in _DJ_INPUTS],
            output_names=list(DJ_STEM_NAMES),
            output_types=[graph.AUDIO, graph.AUDIO, graph.AUDIO, graph.AUDIO],
        )
        dag.nodes.append(graph.DAGNode(
            id=fold_id,
            type="studio_dj_fold",
            inputs=fold_inputs,
            data={},
            title=fold_id,
            signature=signature,
        ))
        nodes[fold_id] = dag.nodes[-1]

        for node in list(dag.nodes):
            if str(node.id) == fold_id:
                continue
            for link in node.inputs or []:
                if link is None or str(link.source_node_id) != str(sep.id):
                    continue
                if int(link.source_slot) % 2 != 0:
                    continue
                stem_index = int(link.source_slot) // 2
                if stem_index < 0 or stem_index >= len(stems):
                    continue
                link.source_node_id = fold_id
                link.source_slot = _role_slot(stems[stem_index])

        saves_by_slot: dict[int, list[Any]] = {}
        for node in list(dag.nodes):
            if str(getattr(node, "type", "")) not in _SAVE_NODE_TYPES:
                continue
            audio_link = node.inputs[0] if node.inputs else None
            if audio_link is None or str(audio_link.source_node_id) != fold_id:
                continue
            saves_by_slot.setdefault(int(audio_link.source_slot), []).append(node)

        display_by_slot = {0: "Vocals", 1: "Melody", 2: "Bass", 3: "Drums"}
        role_source = {}
        for stem in stems:
            role = dj_role(stem) or "melody"
            role_source.setdefault(role, stem)
        kept: dict[str, Any] = {}
        removed: set[str] = set()
        for slot, display in display_by_slot.items():
            candidates = saves_by_slot.get(slot) or []
            if not candidates:
                continue
            preferred = role_source.get({"Vocals": "vocals", "Melody": "melody", "Bass": "bass", "Drums": "drums"}[display])
            chosen = candidates[0]
            if preferred:
                for candidate in candidates:
                    if str(candidate.id).endswith(f":{preferred}"):
                        chosen = candidate
                        break
            for candidate in candidates:
                if candidate is not chosen:
                    removed.add(str(candidate.id))
            new_id = f"save:{step_id}:{display}"
            if str(chosen.id) != new_id:
                _retarget(dag, str(chosen.id), new_id)
            kept[display] = chosen

        if kept and len(kept) < 4:
            template = next(iter(kept.values()))
            widgets = list(_widget_list(template))
            for slot, display in display_by_slot.items():
                if display in kept:
                    continue
                new_id = f"save:{step_id}:{display}"
                audio_link = graph.DAGLink(
                    link_id=link_id,
                    source_node_id=fold_id,
                    source_slot=slot,
                    target_node_id=new_id,
                    target_slot=0,
                    type=graph.AUDIO,
                )
                link_id += 1
                clone = graph.DAGNode(
                    id=new_id,
                    type="pymss_save_audio",
                    inputs=[audio_link, None, None, None, None, None, None, None],
                    data={"widgets_values": widgets},
                    title=new_id,
                    signature=getattr(template, "signature", signature),
                )
                dag.nodes.append(clone)
                kept[display] = clone

        if removed:
            dag.nodes = [node for node in dag.nodes if str(node.id) not in removed]

        if kept:
            names = step.get("output_names") if isinstance(step.get("output_names"), dict) else {}
            step["save"] = {display: "Default" for display in DJ_STEM_NAMES}
            step["output_names"] = {
                display: _dj_output_template(names, role_source.get(role))
                for display, role in (
                    ("Vocals", "vocals"),
                    ("Melody", "melody"),
                    ("Bass", "bass"),
                    ("Drums", "drums"),
                )
            }

        for stem in stems:
            role = dj_role(stem) or "melody"
            overrides[f"{step_id}.{stem}".casefold()] = (fold_id, _DJ_SLOT[role])
        for slot, display in display_by_slot.items():
            overrides[f"{step_id}.{display}".casefold()] = (fold_id, slot)
        nodes = {str(node.id): node for node in dag.nodes}

    return overrides


def ensure_dj_fold_node(graph: Any) -> None:
    """Register the advanced-editor DJ fold node once per process."""
    getter = getattr(graph, "get_node_type", None)
    if getter is not None:
        try:
            getter("studio_dj_fold")
            return
        except Exception:
            pass
    if not all(hasattr(graph, name) for name in (
        "register_node", "NodeSignature", "PortSpec", "AUDIO", "NodeResult", "audio_to_numpy", "numpy_to_audio",
    )):
        return

    def signature(_node: Any) -> Any:
        return graph.NodeSignature(
            inputs=[graph.PortSpec(name, graph.AUDIO) for name in _DJ_INPUTS],
            output_names=list(DJ_STEM_NAMES),
            output_types=[graph.AUDIO, graph.AUDIO, graph.AUDIO, graph.AUDIO],
        )

    def execute(_ctx: Any, inputs: dict[str, Any]) -> Any:
        from stem_delivery import fold_dj_stems, linear_resample

        mix_art = inputs.get("mix")
        if mix_art is None or any(inputs.get(name) is None for name in ("vocals", "bass", "drums")):
            raise StemDeliveryError("4-stem DJ output needs mix, vocals, bass, and drums inputs.")
        mix, sample_rate = graph.audio_to_numpy(mix_art)
        parts: dict[str, Any] = {}
        for name in ("vocals", "bass", "drums"):
            audio, part_rate = graph.audio_to_numpy(inputs[name])
            if int(part_rate) != int(sample_rate):
                audio = linear_resample(audio, int(part_rate), int(sample_rate))
            parts[name] = audio
        folded = fold_dj_stems(mix, parts, mix_layout="channels", stem_layout="channels")
        source = str(getattr(mix_art, "source_path", "") or "")
        outputs = {
            index: graph.numpy_to_audio(folded[name], int(sample_rate), stem_name=name, source_path=source)
            for index, name in enumerate(DJ_STEM_NAMES)
        }
        return graph.NodeResult(outputs=outputs)

    graph.register_node("studio_dj_fold", signature=signature, execute=execute)


def _copy_record(record: Any, **changes: Any) -> Any:
    if is_dataclass(record):
        return replace(record, **changes)
    clone = copy.copy(record)
    for key, value in changes.items():
        setattr(clone, key, value)
    return clone


class SavedAudioResult(list):
    """Path list that still carries pymss save records after OGG replacement."""

    def __init__(self, paths: list[str], records: list[Any]):
        super().__init__(paths)
        self.records = records


def encode_deferred_ogg(saved: Any, plan: OggSavePlan, audio_params: Any) -> Any:
    """Replace deferred float WAV saves with Vorbis, one gain per source mix."""
    if not plan.node_quality:
        return saved
    records = list(getattr(saved, "records", None) or [])
    paths = [str(path) for path in saved if path is not None and str(path).strip()]
    if not records or len(records) != len(paths):
        return saved
    selected: list[tuple[int, Any]] = []
    for index, record in enumerate(records):
        node_id = str(getattr(record, "node_id", "") or "")
        if node_id in plan.node_quality:
            selected.append((index, record))
    if not selected:
        return saved

    groups: dict[str, list[tuple[int, Any]]] = {}
    for index, record in selected:
        source = str(getattr(record, "source_path", "") or "") or f"file:{record.path}"
        groups.setdefault(source, []).append((index, record))

    updated_records = list(records)
    updated_paths = list(paths)
    fallback_quality = resolve_ogg_quality(audio_params)
    for members in groups.values():
        loaded = []
        for _index, record in members:
            audio, sample_rate = load_audio_channels(record.path)
            loaded.append((str(getattr(record, "stem", "") or Path(record.path).stem), audio, sample_rate))
        targets = [Path(record.path).with_suffix(".ogg") for _index, record in members]
        # A group can mix per-node qualities. Encode at each node's quality
        # after the shared gain, so the ceiling stays common.
        qualities = [plan.quality_for(str(record.node_id), fallback_quality) for _index, record in members]
        if len(set(qualities)) == 1:
            encode_stem_group(
                loaded,
                targets,
                output_format="ogg",
                audio_params={**(audio_params or {}), "ogg_quality": qualities[0], "ogg_min_bitrate": 0},
            )
        else:
            from stem_delivery import conform_44k_stereo, shared_gain
            import numpy as np

            prepared = [conform_44k_stereo(audio, sample_rate, layout="channels") for _name, audio, sample_rate in loaded]
            gain = shared_gain(prepared)
            if gain is not None:
                prepared = [np.ascontiguousarray(audio * np.float32(gain), dtype=np.float32) for audio in prepared]
            from stem_delivery import write_vorbis

            for audio, target, quality in zip(prepared, targets, qualities):
                write_vorbis(target, audio, quality)
        for (index, record), target in zip(members, targets):
            Path(record.path).unlink(missing_ok=True)
            updated_paths[index] = str(target)
            updated_records[index] = _copy_record(
                record,
                path=str(target),
                format="ogg",
                sample_rate=44100,
            )
    return SavedAudioResult(updated_paths, updated_records)
