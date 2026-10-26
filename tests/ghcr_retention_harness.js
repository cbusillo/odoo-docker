const fs = require("node:fs");

const [scriptPath, scenarioPath] = process.argv.slice(2);
const script = fs.readFileSync(scriptPath, "utf8");
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));

const owner = "example-owner";
const packageName = process.env.PACKAGE_NAME;
const imageRoot = `ghcr.io/${owner}/${packageName}@`;
const deletedIds = [];
let failure = null;

const requireThisPackage = (route, parameters) => {
    if (!route.includes("/users/{username}/") || parameters.username !== owner || parameters.package_name !== packageName) {
        throw new Error(`Request outside this package: ${route} ${JSON.stringify(parameters)}`);
    }
};

const github = {
    paginate: async (route, parameters) => {
        requireThisPackage(route, parameters);
        return structuredClone(scenario.versions);
    },
    request: async (route, parameters) => {
        requireThisPackage(route, parameters);
        if (!route.startsWith("DELETE ")) {
            throw new Error(`Unexpected request route: ${route}`);
        }
        deletedIds.push(parameters.package_version_id);
    },
};

const core = {
    info: () => {},
    warning: () => {},
    setFailed: (message) => {
        failure = String(message);
    },
};

const exec = {
    getExecOutput: async (command, args) => {
        const imageReference = args[args.length - 1];
        if (command !== "docker" || !imageReference.startsWith(imageRoot)) {
            throw new Error(`Inspection outside this package: ${command} ${args.join(" ")}`);
        }
        const digest = imageReference.slice(imageRoot.length);
        const inspection = args.includes("--raw")
            ? scenario.rawInspections[digest]
            : scenario.childInspections?.[digest];
        return { exitCode: 1, stdout: "", stderr: "", ...inspection };
    },
};

const context = {
    repo: { owner },
    payload: { repository: { owner: { type: "User" } } },
};

const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;

(async () => {
    await new AsyncFunction("github", "context", "core", "exec", "require", script)(github, context, core, exec, require);
    process.stdout.write(JSON.stringify({ deletedIds, failure }));
})();
