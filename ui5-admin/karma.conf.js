module.exports = function (config) {
    config.set({
        frameworks: ["ui5"],
        ui5: {
            configPath: "ui5-local.yaml",
            testpage: "webapp/test/testsuite.qunit.html"
        },
        browsers: ["ChromeHeadless"],
        singleRun: true,
        reporters: ["progress"]
    });
};
