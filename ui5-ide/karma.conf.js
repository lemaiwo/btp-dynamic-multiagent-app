module.exports = function (config) {
    config.set({
        frameworks: ["ui5"],
        ui5: {
            configPath: "ui5-local.yaml",
            testpage: "webapp/test/testsuite.qunit.html"
        },
        // A desktop-sized window: the workbench is a three-pane desktop
        // layout, and at the 800x600 default the editor pane's toolbar moves
        // its buttons into the overflow menu.
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
