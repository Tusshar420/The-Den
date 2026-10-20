"""
Example module. Shows the three things a module can register:

    a tool      hello.greet(name?)  - for the assistant, once granted in tools.json / agents.json
    a command   /hello [name]       - POST /modules/commands/hello
    a listener  TASK_COMPLETED      - counts finished tasks

It needs no secrets and declares no capabilities (its tool only returns text).
"""

from assistant.tools import ActionSpec, Prepared, Tool
from den_modules import events


class HelloTool(Tool):
    id = "hello"
    label = "Hello"
    actions = {"greet": ActionSpec("greet", "Greet someone by name.", {"name?": "who to greet"})}

    def __init__(self, greeting: str):
        self.greeting = greeting

    def prepare_greet(self, args, ctx):
        return Prepared({"name": args.get("name", "world")}, [], args.get("name", "world"), "Saying hello")

    def run_greet(self, args, ctx):
        text = f"{self.greeting}, {args['name']}!"
        return {"text": text, "data": {"greeting": text}}


class Module:
    def initialize(self, ctx):
        self.ctx = ctx
        self.greeting = ctx.config["greeting"]
        self.completed = ctx.get_state("completed", 0)
        ctx.register_tool(HelloTool(self.greeting))
        ctx.register_command("hello", self.command, "Say hello: /hello [name]")
        ctx.subscribe(events.TASK_COMPLETED, self.on_task_completed)

    def start(self):
        self.ctx.log.info("hello module ready (%d tasks completed so far)", self.completed)

    def command(self, arg: str) -> str:
        return f"{self.greeting}, {arg.strip() or 'world'}!"

    def on_task_completed(self, event):
        self.completed += 1
        self.ctx.set_state("completed", self.completed)
        self.ctx.log.info("task %s completed (%d seen)", event["task_id"], self.completed)

    def stop(self):
        self.ctx.log.info("hello module stopping")
