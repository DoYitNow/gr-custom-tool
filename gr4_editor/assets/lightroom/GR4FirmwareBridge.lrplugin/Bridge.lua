-- Runs inside Lightroom Classic. Only adapter-generated Lua data tables are
-- loaded from the managed task directory; input XMP is never executed as code.
local LrApplication = import "LrApplication"
local LrDate = import "LrDate"
local LrExportSession = import "LrExportSession"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrTasks = import "LrTasks"
local Bridge = { running = false }
local PLUGIN_VERSION = "0.3.0"
local RENDER_RECIPE_VERSION = 3

local function escape(text)
    return '"' .. tostring(text):gsub('[%z\1-\31\\"]', function(c)
        if c == '"' then return '\\"' end
        if c == '\\' then return '\\\\' end
        return string.format("\\u%04x", string.byte(c))
    end) .. '"'
end

local function json(value)
    if value == nil then return "null" end
    if type(value) == "string" then return escape(value) end
    if type(value) == "number" or type(value) == "boolean" then return tostring(value) end
    assert(type(value) == "table", "Cannot serialize bridge result")
    local list, count = {}, 0
    for key, _ in pairs(value) do
        count = count + 1
        if type(key) ~= "number" then count = -1; break end
    end
    if count > 0 then
        for i = 1, count do list[#list + 1] = json(value[i]) end
        return "[" .. table.concat(list, ",") .. "]"
    end
    for key, item in pairs(value) do list[#list + 1] = escape(key) .. ":" .. json(item) end
    return "{" .. table.concat(list, ",") .. "}"
end

local function write(path, value)
    local file, message = io.open(path, "wb")
    assert(file, message)
    file:write(json(value)); file:close()
end

local function version()
    if type(LrApplication.versionString) == "function" then return LrApplication.versionString() end
    return tostring(LrApplication.versionTable().major)
end

local function config()
    local path = LrPathUtils.child(_PLUGIN.path, "Config.lua")
    local chunk, message = loadfile(path)
    assert(chunk, message or "Bridge configuration missing")
    return chunk()
end

local function render(request)
    assert(request.schema_version == 3, "Unsupported bridge request")
    assert(request.render_recipe_version == RENDER_RECIPE_VERSION, "Unsupported render recipe; restart plugin")
    assert(request.profile_uuid and request.develop_settings.Look.UUID == request.profile_uuid, "Profile request mismatch")
    local catalog = LrApplication.activeCatalog()
    assert(catalog, "Lightroom Classic catalog not open")
    local preset
    for attempt = 1, 120 do
        for _, folder in ipairs(LrApplication.developPresetFolders()) do
            for _, candidate in ipairs(folder:getDevelopPresets()) do
                local path = candidate:getFile()
                if path and LrPathUtils.leafName(path):lower() == request.native_preset_filename:lower() then
                    preset = candidate; break
                end
            end
            if preset then break end
        end
        if preset then break end
        LrTasks.sleep(0.25)
    end
    assert(preset, "Native XMP render preset was not loaded; import profiles/presets or restart Classic")
    local photos, inputs = {}, {}
    for _, sample in ipairs(request.samples) do
        local photo = catalog:findPhotoByPath(sample.path)
        catalog:withWriteAccessDo("Firmware editor calibration input", function()
            if not photo then photo = catalog:addPhoto(sample.path) end
            assert(photo, "DNG import failed: " .. sample.name)
            photo:applyDevelopPreset(preset)
        end, { timeout = 30 })
        photos[#photos + 1] = photo
        inputs[photo] = sample
    end
    -- Develop changes can be asynchronous. Require actual UUID readback before
    -- starting exports; an unsupported profile application is a failed job.
    for _, photo in ipairs(photos) do
        local matched = false
        for attempt = 1, 120 do
            local settings = photo:getDevelopSettings()
            if settings.Look and settings.Look.UUID == request.profile_uuid then
                matched = true
                local parameters = settings.Look.Parameters or {}
                for key, value in pairs(request.profile_tables) do
                    if parameters[key] ~= value then matched = false end
                end
                if matched then break end
            end
            LrTasks.sleep(0.25)
        end
        assert(matched, "Applied Look UUID or RGB/Look table did not match; profile may require a Classic restart")
    end
    local session = LrExportSession {
        photosToExport = photos,
        exportSettings = {
            LR_format = "JPEG", LR_jpeg_quality = 1, LR_export_colorSpace = "sRGB",
            LR_export_destinationType = "specificFolder", LR_export_destinationPathPrefix = request.output_directory,
            LR_export_useSubfolder = false, LR_collisionHandling = "overwrite", LR_renamingTokensOn = false,
            LR_size_doConstrain = false, LR_outputSharpeningOn = false,
            LR_minimizeEmbeddedMetadata = false, LR_embeddedMetadataOption = "all",
        },
    }
    local outputs = {}
    for _, rendition in session:renditions({ stopIfCanceled = true }) do
        local success, path = rendition:waitForRender()
        assert(success, tostring(path))
        local sample = inputs[rendition.photo]
        assert(sample, "Unknown exported photo")
        local settings = rendition.photo:getDevelopSettings()
        assert(settings.Look and settings.Look.UUID == request.profile_uuid, "Look changed while exporting")
        for key, value in pairs(request.profile_tables) do
            assert((settings.Look.Parameters or {})[key] == value, "Applied color table disappeared during actual rendering")
        end
        outputs[#outputs + 1] = { name = sample.name, path = path, look_uuid = settings.Look.UUID }
    end
    assert(#outputs == #request.samples, "Incomplete Lightroom export")
    return { schema_version = 3, status = "rendered", job_id = request.job_id, nonce = request.nonce,
        render_engine = "lightroom", render_recipe_version = RENDER_RECIPE_VERSION,
        xmp_sha256 = request.xmp_sha256, profile_uuid = request.profile_uuid, profile_tables = request.profile_tables,
        application = { name = "Adobe Lightroom Classic", version = version(), plugin_version = PLUGIN_VERSION },
        native_preset_path = preset:getFile(), native_preset_sdk_uuid = preset:getUuid(),
        completed_at = LrDate.currentTime(), outputs = outputs }
end

local function tick(settings)
    if not LrFileUtils.exists(settings.jobs_directory) then return end
    for folder in LrFileUtils.directoryEntries(settings.jobs_directory) do
        local requestPath = LrPathUtils.child(folder, "render-request.lua")
        local resultPath = LrPathUtils.child(folder, "render-result.json")
        if LrFileUtils.exists(requestPath) and not LrFileUtils.exists(resultPath) then
            local request
            local ok, result = LrTasks.pcall(function()
                request = assert(loadfile(requestPath))()
                write(settings.status_path, { status = "rendering", job_id = request.job_id,
                    application_version = version(), plugin_version = PLUGIN_VERSION, heartbeat = LrDate.currentTime() })
                return render(request)
            end)
            if not ok then result = { status = "failed", message = tostring(result), job_id = request and request.job_id,
                nonce = request and request.nonce, xmp_sha256 = request and request.xmp_sha256 } end
            write(resultPath, result)
        end
    end
end

function Bridge.start()
    if Bridge.running then return end
    Bridge.running = true
    LrTasks.startAsyncTask(function()
        local settings = config()
        while Bridge.running do
            local ok, message = LrTasks.pcall(function() tick(settings) end)
            write(settings.status_path, { status = ok and "connected" or "error", message = not ok and tostring(message) or nil,
                application_version = version(), plugin_version = PLUGIN_VERSION, heartbeat = LrDate.currentTime() })
            LrTasks.sleep(2)
        end
    end)
end

function Bridge.stop() Bridge.running = false end
return Bridge
