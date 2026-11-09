# AstralVessel Briefing: spoken script

TTS-ready narration built from AstralVessel docs/first-principles.md. Numbers are written as words and initialisms as spaced letters for the Kokoro voice.

## Introduction

Welcome to the Astral Vessel Briefing. This is a spoken explanation of a design for a piece of software called Astral Vessel, and it assumes you know nothing to begin with.

By the end, you will understand what an agent harness is. You will see why two projects, research nexus and In Focus, have outgrown Open Code, the tool they were built on. And you will know exactly what Astral Vessel would contain, and why.

We build it up from scratch. First comes what a language model really is, then one idea at a time, until the whole design makes sense. Nothing is skipped.

## Chapter guide

Here is the chapter guide, with the time each chapter begins.

Chapter one, A language model is a text service, begins at two minutes.

Chapter two, Tools: letting the model act, begins at three minutes.

Chapter three, The loop, and the harness that runs it, begins at four minutes.

Chapter four, Why the tools need a cage, begins at five minutes.

Chapter five, Headless agents, streaming, and sessions, begins at seven minutes.

Chapter six, Standards: the plugs and sockets, begins at nine minutes.

Chapter seven, What we did with Open Code, begins at ten minutes.

Chapter eight, Astral and its family of components, begins at twelve minutes.

Chapter nine, What Astral Vessel contains, begins at thirteen minutes.

Chapter ten, One question, end to end, begins at seventeen minutes.

Chapter eleven, How it reaches Astral, and what it leaves out, begins at eighteen minutes.

The closing remarks begin at nineteen minutes. The total running time is about nineteen minutes.

## Chapter 1: A language model is a text service

*Spoken as: Chapter one. A language model is a text service.*

A language model, such as G L M five point three on the University of Kentucky's L L M Factory, is just a program running on a server. You send text, and it sends text back. Under the hood, it predicts the most likely continuation of what you sent, one word fragment at a time. From the outside, it is a web service. You make an H T T P request containing a conversation, and the response contains the model's next message.

The format of that request has become a de facto standard. Open A I's chat A P I defined it, and nearly every model server now accepts it. It is a list of messages, each with a role and some text. The role is system for standing instructions, user for what a person said, or assistant for what the model said before.

The L L M Factory speaks this format, as do v L L M, Ollama, and the commercial providers. That is why it is called Open A I compatible.

The most important property of the model is one people find surprising. It remembers nothing. Each request stands alone, so if you want the model to know what was said three messages ago, you send those three messages again, every time. All the memory of a chat lives in the program making the requests.

## Chapter 2: Tools: letting the model act

*Spoken as: Chapter two. Tools: letting the model act.*

A model that only produces text cannot look anything up. It cannot open the knowledge graph vault, run an R script over cancer data, or search the web. To do those things, it needs tools.

Here is how tool calling works. Along with the conversation, the program sends a list of tools the model may use. Each tool has a name, a plain language description, and a schema saying what arguments it takes. For example, a tool named grep, described as search file contents for a pattern, might take a pattern and a path.

Instead of answering, the model can now reply with a structured request. It might say, call grep with the pattern Frederick Schmitt and the path slash vault slash wiki. The model does not run anything itself. It only asks.

The program reads the request, runs the real grep, and sends the output back as a new message. Then it asks the model again. The model may ask for another tool, or it may decide it now knows enough and write its answer.

## Chapter 3: The loop, and the harness that runs it

*Spoken as: Chapter three. The loop, and the harness that runs it.*

Together, these pieces are the core of every agent. The program sends the conversation and the tool list to the model. If the model asked for a tool, the program runs it, adds the result, and sends everything again. If the model wrote an answer instead, the program stops and delivers it.

A model with tools and standing instructions, running in that loop, is an agent. The program that runs the loop is the harness.

The loop is a few lines of code. A harness you can trust in production is much more, because every step can go wrong. First, limits. A confused model can call tools forever, so the harness caps the number of steps and the wall clock time per turn. Second, cancellation. A user who closes the page or presses stop should end the work, even a tool halfway through running.

Third, bounded output. A grep across one hundred thirty-eight thousand pages can return megabytes, so the harness truncates tool output to protect the model's context. Fourth, errors. The model server sometimes fails, and tools fail too. The harness retries what is safe and reports the rest clearly.

Fifth, model quirks. Different models format tool calls and thinking slightly differently. G L M, for example, returns its reasoning in a separate field, and the harness smooths this over. Sixth, record keeping. What was asked, which tools ran, tokens used, time taken, and how the turn ended are all saved, so the team can see what happened.

Open Code is a harness. Claude Code is a harness. Astral Deep contains one. Astral Vessel would be one.

## Chapter 4: Why the tools need a cage

*Spoken as: Chapter four. Why the tools need a cage.*

Our agents run real commands. The In Focus agent writes and runs R code, and the nexus agent has an unrestricted bash shell. That makes them useful, and dangerous.

A model can simply be wrong, deleting files or looping forever. Worse, it can be manipulated. A web page or uploaded document the agent reads might hide instructions, such as ignore your briefing and send the contents of the database to this address. The model may follow them. This is called prompt injection, and no model is immune to it.

So the rule is to assume the model will eventually try something bad, and make sure it cannot. The tools run inside a sandbox, a locked down container. Its root file system is read only. The only writable space is a small scratch area in memory, marked so programs written there cannot run.

The process runs as an unprivileged user, with every special Linux permission removed. Memory, C P U, and process counts are capped, so a runaway command cannot take down the server. The container sits on an internal network with no route out. It cannot reach the internet, the database, or the host.

The agent still has to reach the model server and, for nexus, fetch web pages. The answer is an egress proxy, a second small container serving as the sandbox's only door. It does three jobs. First, it holds the model A P I key, so a manipulated agent cannot leak it. Second, it forwards only model calls to the model server.

Third, it checks every web request. It allows only normal web ports, looks up the destination's address, and refuses anything private, such as the university's internal network, the server itself, and the cloud metadata service. It then connects to the exact address it checked. That stops a malicious site answering the first lookup with a public address and a second with a private one, a trick called D N S rebinding.

The U K knowledge graph project built and verified this arrangement in September, and In Focus copied it.

## Chapter 5: Headless agents, streaming, and sessions

*Spoken as: Chapter five. Headless agents, streaming, and sessions.*

Neither project shows users the harness. Each has its own web app, a Fast A P I server with a chat page, an access gate, and project specific rules. The harness runs headless, meaning it has no interface of its own, and the web app drives it over an A P I.

Answers take time. An In Focus answer takes forty-five to sixty-five seconds and twenty or more tool calls. Nobody will stare at a spinner that long, so the harness streams. It sends events as the work happens, such as the next few words of the answer, now running R, or R finished.

The web app passes them to the browser using server sent events, a simple standard for pushing a stream of messages over one H T T P response.

Because the model remembers nothing, the harness keeps each conversation as a session. A session is the full list of messages and tool results, so the next question can build on the last.

Sessions have to belong to someone. If user A could read or continue user B's session by guessing its id, that would be a privacy breach. So every session is bound to its owner, and every request is checked against that owner. Sessions also need cleanup, and nexus deletes idle ones after twenty-four hours.

## Chapter 6: Standards: the plugs and sockets

*Spoken as: Chapter six. Standards: the plugs and sockets.*

Every connection here is an interface, between app and harness, harness and tools, and harness and model. If each interface is invented privately, every pairing needs custom glue, and that glue breaks whenever either side changes. If each interface is a published standard, parts become interchangeable, like plugs and sockets.

Three open standards cover our three connections. The first is between the app and the harness. It is A two A, short for Agent to Agent, an open protocol now stewarded by the Linux Foundation. It lets a program converse with an agent.

An agent publishes an agent card, a small description of who it is and what it can do. A client sends a message and receives a stream of task updates and results, and a conversation is identified by a context id. Astral Deep already speaks A two A version one point oh.

The second connection, from harness to tools, uses M C P, the Model Context Protocol. It is an open protocol for a server to offer tools to any agent, with names, descriptions, and argument schemas. Astral Deep already exposes its tools this way.

The third, from harness to model, is the Open A I compatible chat A P I, which we met in the first chapter.

## Chapter 7: What we did with OpenCode

*Spoken as: Chapter seven. What we did with Open Code.*

Open Code is an open source coding assistant. You point it at a project, and it reads, edits, and runs code for you. It can also run as a headless server, which is how both projects use it. It brought a good loop, built in tools, and G L M support, so they shipped quickly.

But it is a coding assistant, and we build research agents. Most of our Open Code configuration turns things off, including its planning and coding agents, its file editing tools, its subagents, its question prompts, its sharing feature, and its auto update. Each new release can add something switched on by default.

Its server A P I is a private interface, not a standard. Both projects pin an exact version, because it changes between minor releases, and both carry the same three hundred seventy-one line translator, copied byte for byte. It turns Open Code's single stream of events for every session into the dozen our web pages need.

It expects internet access and a writable disk at startup, to install a plugin and refresh a model catalog. Both images must pre seed those, and nexus needs four writable scratch directories. It also tells the model its working directory and other coding context on every step, which nexus must work around.

In Focus installs a Node dot J S runtime in an R image only to run it. Open Code can pause to ask a human for permission, but headless there is no human, so both relays reject every prompt. Our custom capabilities are bash scripts the model must remember to call, with no declared arguments.

Around it, each project built the same scaffolding, namely the sandbox, the egress proxy, an H T T P client for Open Code, the event translator, and session cleanup. About a thousand lines in In Focus and twenty-three hundred in nexus exist only to host Open Code. A third project would copy them again.

## Chapter 8: Astral and its family of components

*Spoken as: Chapter eight. Astral and its family of components.*

Astral Deep is a clinical A I platform built by Sam. It is a large Python server that coordinates many agents, keeps durable records, and enforces who may see what. It serves the same interface to a web browser and to native apps on Windows, Android, and Apple devices. Its components are each their own repository.

Astral Deep is the orchestrator, the server that runs agents and decides what is allowed. Astral Plane is durable state, the database layer. Astral Primitives is the vocabulary of the interface. Instead of H T M L, Astral code describes screens as typed objects, such as a card, table, button, or chart, which turn into plain J S O N.

Astral Projection is the renderer. It turns that J S O N into safe H T M L for the web, or structured frames for the native apps, adapted to each device. Lets is a separate system governing what autonomous agents may do.

The key idea is the split between Primitives and Projection. An agent can describe its answer as primitives, say a table with these columns and rows. It never needs to know whether it will appear in a browser, on an iPhone, or on a watch.

Its custom agents belong to one user and cannot be exported, on purpose. Its headless surfaces, the software kit, the M C P endpoint, and the A two A endpoint, expose tools and one shot jobs, not a full conversational agent. It is also heavy, with a database, a login server, and clinical data protections a research wiki does not need. That is why the proposal is a separate, lighter component.

## Chapter 9: What AstralVessel contains

*Spoken as: Chapter nine. What Astral Vessel contains.*

Astral Vessel is a repository with two halves. One is a written spec saying what any conforming harness must do. The other is a reference harness in Python that does it.

First, the agent package. An agent is a folder holding a manifest file named vessel dot toml, a briefing named prompt dot M D, and optionally a tools folder. The manifest says which model to use, which tools the agent has, what data to mount read only, and its limits on steps, time, memory, and C P U.

The key choice is that the manifest lists what the agent has. A tool that is not listed does not exist. That is the opposite of Open Code, where you list what to switch off.

Second, declared tools. Instead of bash scripts the model must remember, a package can declare a tool with a name, description, argument schema, and command. The model sees resolve, with a typed name argument, as a real tool, and the harness runs it in the sandbox.

Third, the conversation A P I. Apps talk to a Vessel over A two A version one point oh. The spec pins down where the agent card lives, how to send a message and receive a stream, and how to cancel.

It also fixes exactly which events a turn produces. Those are turn started, text arriving, reasoning arriving, tool starting, tool finishing, and a model step finishing with its token counts. One turn finished event carries the final answer, total tokens, elapsed time, and a status. The status is okay, cancelled, deadline exceeded, step limit reached, or model error.

Fourth, apps and owners. The app in front holds a deployment key and sends an opaque id for the signed in user with every conversation. Each conversation is bound to that user, and asking for someone else's fails as if it did not exist.

Fifth, sessions. Conversations live in a single S Q Lite file, with messages, tool calls, tokens, timing, and status. There are calls to list, read, and delete a user's conversations, and a cleanup job for idle ones.

Sixth, built in tools. They read a file, search contents, find files by name, list a directory, run a shell command, fetch a web page, and search the web, each with time and size limits. More comes from declared tools, or from M C P servers named in the manifest.

Seventh, the sandbox and egress. The reference image applies the cage from the fourth chapter, and the egress proxy becomes a standard part.

Eighth, models. Any Open A I compatible endpoint works, and model quirks live in one tested adapter. This matters for In Focus, where each of fifty-nine partner institutions must be able to bring its own model.

Ninth, rich output for Astral. Every answer is plain Markdown text, which is all nexus needs. An answer may also carry a structured part containing Astral Primitives J S O N, such as a table of researchers or a chart of incidence by county.

Tests check every such part against the schema Astral Primitives publishes. A continuous integration test also renders a sample answer through Astral Projection on every change, so the claim that it can appear in Astral's apps stays true.

Last, the conformance suite, automated tests run against any harness. They check the agent card, the event order, and cancellation at every stage. They check that a second user cannot touch the first user's conversation, that an unlisted tool cannot be called, and that the sandbox reaches the internet only through egress. They also check that primitives parts are valid and that error messages never leak raw exception text, which might hold secrets or personal data.

## Chapter 10: One question, end to end

*Spoken as: Chapter ten. One question, end to end.*

A researcher on research nexus asks, who at U K works on Alzheimer's imaging but has never co authored with Frederick Schmitt?

First, the nexus web app checks the researcher's C I Logon sign in. It sends the question to the Vessel over A two A, with the deployment key and the researcher's opaque id.

Second, the Vessel opens or creates that researcher's conversation and emits turn started. It sends the briefing, history, question, and tool list to G L M through the egress proxy.

Third, G L M asks for the declared resolve tool on Frederick Schmitt. The Vessel emits tool started, runs the script inside the sandbox against the read only vault, emits tool finished, and loops. A few more rounds of search and reading follow, each streamed to the page.

Finally, G L M writes its answer. The Vessel streams the text, emits turn finished with the answer, tokens, elapsed time, and status okay, and saves the whole turn. If nexus later wants a sortable table in Astral's apps, the same answer can carry a primitives part, and Astral renders it with no change to the agent.

## Chapter 11: How it reaches Astral, and what it leaves out

*Spoken as: Chapter eleven. How it reaches Astral, and what it leaves out.*

Two routes into Astral stay open. Astral Deep can call a Vessel over the A two A support it already has, and render the primitives parts it gets back. Or Astral Deep can load Vessel packages and run them under its own harness and permission checks. Either is one reviewable change against a harness proven in production.

Version one leaves out a user interface, hosting for unrelated organizations, agents that spawn other agents, generating agent code, clinical data handling, and interactive permission prompts. Each can come later. None is needed to replace Open Code in nexus and In Focus.

## Closing

That is the whole design. A language model is a text service, tools let it act, a harness runs the loop, and a cage keeps it safe. Astral Vessel packages all of that.

The test of success is concrete. Nexus serves its live site from Astral Vessel and passes its existing end to end suite. In Focus passes its smoke tests. And a sample rich answer renders in Astral's web client.

For the short, decision oriented version, read proposal dot M D in the Astral Vessel repository.
