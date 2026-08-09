# Model context measurements

Measured with `python -m wyzer.dev.measure_context --json` on 2026-08-09. The
utility builds the normal registry and empty-state system prompt, serializes the 48
model-visible capability tools plus three task-engine tools with compact JSON, and
estimates tokens as `characters / 4`.

| Metric | Before | After | Reduction |
|---|---:|---:|---:|
| System prompt | 6,164 | 3,849 | 2,315 (37.6%) |
| Tool schemas | 29,004 | 20,929 | 8,075 (27.8%) |
| Total | 35,168 | 24,778 | 10,390 (29.5%) |
| Approximate tokens | 8,792 | 6,194 | 2,598 (29.5%) |

No model-visible tool was removed or hidden. Argument types, names, required fields,
defaults, constraints, enums, validation, execution, and results are unchanged.
Model-facing schemas remain derived from the registered Pydantic argument models.
Only redundant Pydantic display `title` keys are removed during native-schema
serialization.

## Per-pack schema characters

These are sums of each serialized tool object in a pack; the total schema measurement
also includes the enclosing list and separators.

| Pack | Before | After |
|---|---:|---:|
| applications | 1,709 | 1,164 |
| audio | 3,163 | 2,132 |
| browser | 5,765 | 4,300 |
| clipboard | 1,866 | 1,291 |
| desktop_interaction | 1,600 | 1,227 |
| diagnostics | 796 | 430 |
| files | 5,334 | 4,062 |
| media | 687 | 526 |
| perception | 1,726 | 1,169 |
| system | 847 | 640 |
| task_engine | 2,544 | 1,865 |
| windows | 2,915 | 2,071 |

## Largest schemas

| Before | Characters | After | Characters |
|---|---:|---|---:|
| open_indexed_folder | 1,346 | open_indexed_folder | 1,017 |
| control_application_audio | 1,301 | control_application_audio | 905 |
| task_plan_create | 1,069 | task_plan_create | 758 |
| activate_visual_target | 978 | press_desktop_key | 725 |
| control_master_audio | 945 | task_plan_revise | 656 |
| press_desktop_key | 938 | control_master_audio | 653 |
| task_plan_revise | 875 | control_named_window | 604 |
| control_named_window | 845 | activate_visual_target | 602 |
| browser_type_text | 811 | browser_type_text | 574 |
| move_named_window_to_monitor | 798 | inspect_screen | 567 |

The longer remaining schemas are structural rather than prose-heavy: nested monitor
destinations, inherited audio arguments, task step objects, and the desktop-key enum.
Those structures were retained to preserve validation and make tool calls reliable.
