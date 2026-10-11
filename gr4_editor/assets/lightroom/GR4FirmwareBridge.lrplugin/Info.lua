return {
    LrSdkVersion = 13.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = "org.gr4firmware.calibration.bridge",
    LrPluginName = "固件编辑器校色桥接",
    LrPluginInfoUrl = "https://developer.adobe.com/lightroom-classic",
    LrInitPlugin = "Init.lua",
    LrForceInitPlugin = true,
    LrShutdownPlugin = "Shutdown.lua",
    LrLibraryMenuItems = {{ title = "处理校色队列", file = "Menu.lua" }},
    -- The File menu is present when Classic restores the Develop module too.
    LrExportMenuItems = {{ title = "处理校色队列", file = "Menu.lua" }},
    VERSION = { major = 0, minor = 3, revision = 0, build = 1 },
}
