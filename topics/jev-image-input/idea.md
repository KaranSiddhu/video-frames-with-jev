# Context-assisted video-frame localization

These public method notes describe the standalone POC. The application code is a copy of the blogging workspace's POC, with its directory layout preserved.

## Context-assisted question change — 2026-10-19

The operator supplies the scene context that the described man is holding a firearm. Jev is asked whether the described person is visibly localizable, whether there is a unique match, and where the person's full visible body is.

This supplied label changes the task from independent object recognition to context-assisted localization. It does not establish visibility in every frame, authenticity, identity or intent. The input JSON and UI subtitle disclose the context.

## Processing and confidence

The fixed source is sampled at one-second intervals and converted to JPEG images. The application pins `jev-1.14.0`, uses the experimental image header, and reuses one asynchronous client without automatic retries.

A border requires count `one`, visibility probability at least 0.85, structurally valid normalized edges and Box confidence at least 0.80. Missing/low confidence withholds the border. These values are application policy, not an accuracy guarantee.

Video stays paused while its frame is analyzed. The accepted border is drawn from the returned coordinates and cleared before advancing. This is not continuous tracking or a real-time performance claim.

## Reproduction and measurements

Bring your own permitted local video. The repository does not distribute the example media or historical run outputs. New runs automatically save exact frames, prompts, source/code hashes, installed versions, responses, decisions, reported usage and timing.

SDK timing includes serialization, network/upload, service processing and response parsing. Browser end-to-end timing includes frame preparation and the backend round trip. The display hold is excluded. Unknown usage must remain unknown rather than being counted as free.

Confidence or accepted-box counts are not ground-truth accuracy. A broader evaluation requires reference boxes, positive/negative examples, look-alikes and testing separate from prompt development.

See the [root README](../../README.md) and [POC guide](poc/README.md) for setup and the local evidence format.
