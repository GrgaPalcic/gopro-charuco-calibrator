# Docs

What we learned calibrating wide GoPro lenses for robotics, with the evidence behind it. Start
with the doc that answers your question.

| Doc | Answers |
|---|---|
| [lens-modes-and-models.md](lens-modes-and-models.md) | Which GoPro modes can be calibrated, which model fits which field of view, and which capture paths exist (webcam, recording, HDMI, Wi-Fi, Labs) with their limits. |
| [footguns.md](footguns.md) | Why a calibration looks impossible when it isn't. Each trap as symptom → cause → how to tell → fix, plus the claims we had to retract. |
| [measurements.md](measurements.md) | Every result we measured: date, dataset, board, lens mode, solver, error. Also the pending video-vs-webcam comparison. |
| [double-sphere-backend.md](double-sphere-backend.md) | How the app drives OpenICC for the Max Lens Mod, how to read a Double Sphere result, and how to calibrate an on-camera recording. |
| [umi-and-deployment.md](umi-and-deployment.md) | What UMI does, why a Double Sphere result does not drop into it, the HDMI capture chain for a live robot, and multi-camera sync. |

Tags used throughout: **verified** means measured by us or read from the primary source;
**inferred** means consistent with the evidence but not directly demonstrated.

These docs replace the June reports (`calibration-report.html`, `calibration-research-report.md`).
Their corrections are folded in rather than appended; the retracted claims are listed at the end of
[footguns.md](footguns.md#what-we-got-wrong-before).
