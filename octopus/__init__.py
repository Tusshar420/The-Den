"""
Octopus: the layer that sits above the queue.

    request -> understand (plan.py) -> pick agent, model and tools -> check permissions
            -> execute through the existing queue -> validate -> report

    registry.py   agents.json + tools.json, re-read whenever they change
    plan.py       turns a request into a plan (and scores the candidate models)
    control.py    instant commands about Octopus itself: status, stop, continue, backlog
    voice/        speech-to-text and text-to-speech providers

Models live in models.json (router.py); capability policies in assistant/capabilities.py.
"""
