-- Explicit menu use also refreshes the adapter after a local code update.
local runtime = require("Runtime")
local previous = runtime.bridge or require("Bridge")
previous.stop()
local fresh = assert(loadfile(import("LrPathUtils").child(_PLUGIN.path, "Bridge.lua")))()
runtime.bridge = fresh
fresh.start()
