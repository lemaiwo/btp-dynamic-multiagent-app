module.exports = function (config) {
    config.set({
        frameworks: ["ui5"],
        ui5: {
            configPath: "ui5-local.yaml",
            testpage: "webapp/test/testsuite.qunit.html"
        },
        // A fixed desktop window. Headless Chrome opens at 800x600, where what
        // fits in an OverflowToolbar depends on the fonts of the machine: on
        // the CI runner the data catalogue's "Rebuild all" button went into
        // the overflow menu, a journey could not press it, and every journey
        // after that timeout failed with it. Journeys that need a phone set
        // the device themselves.
        browsers: ["ChromeHeadlessDesktop"],
        customLaunchers: {
            ChromeHeadlessDesktop: {
                base: "ChromeHeadless",
                flags: ["--window-size=1440,900"]
            }
        },
        singleRun: true,
        reporters: ["progress"]
    });
};
