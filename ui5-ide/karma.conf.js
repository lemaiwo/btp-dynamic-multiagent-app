module.exports = function (config) {
    config.set({
        frameworks: ["ui5"],
        ui5: {
            configPath: "ui5-local.yaml",
            testpage: "webapp/test/testsuite.qunit.html"
        },
        // A desktop-sized window: the session page shows the conversation and
        // the artifact column side by side, and at the 800x600 default its
        // toolbars move buttons into the overflow menu.
        customLaunchers: {
            ChromeHeadlessDesktop: {
                base: "ChromeHeadless",
                flags: ["--window-size=1600,1000"]
            }
        },
        browsers: ["ChromeHeadlessDesktop"],
        singleRun: true,
        reporters: ["progress"]
    });
};
