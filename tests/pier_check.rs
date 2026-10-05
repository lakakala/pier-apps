use minijinja::{Environment, UndefinedBehavior};
use pier_pkg::{Architecture, PackOptions, ProxyOptions, Stage, inspect, pack, unpack, validate};
use serde_json::{Value, json};
use std::{collections::BTreeMap, env, fs, path::Path};

fn variables() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("API_KEY".into(), "pier-check-client-7bf162".into()),
        (
            "MANAGEMENT_KEY".into(),
            "pier-check-management-25d694".into(),
        ),
        ("PORT".into(), "18317".into()),
    ])
}

fn test_proxy() -> ProxyOptions {
    // Validation does not connect to the placeholder proxy. Package tests serve
    // the official archive locally and explicitly bypass it via no_proxy.
    ProxyOptions {
        http_proxy: Some("http://127.0.0.1:9".into()),
        https_proxy: Some("http://127.0.0.1:9".into()),
        no_proxy: Some("127.0.0.1,localhost".into()),
    }
}

fn environment() -> Environment<'static> {
    let mut env = Environment::new();
    env.set_undefined_behavior(UndefinedBehavior::Strict);
    env.set_keep_trailing_newline(true);
    env
}

fn yaml(path: &Path) -> Value {
    serde_yaml_ng::from_str(&fs::read_to_string(path).unwrap()).unwrap()
}

fn check(root: &Path) -> Value {
    let recipe_dir = root.join("apps/cliproxyapi");
    let recipe = yaml(&recipe_dir.join("pier-pkg.yml"));
    let metadata = inspect(&recipe_dir).unwrap();
    let declared: Vec<_> = metadata.variables.keys().map(String::as_str).collect();
    assert_eq!(declared, ["API_KEY", "MANAGEMENT_KEY", "PORT"]);
    let env = environment();
    let mut downloads = Vec::new();
    for architecture in [Architecture::Amd64, Architecture::Arm64] {
        let mut options = PackOptions::new(architecture);
        options.variables = variables();
        assert!(matches!(
            validate(&recipe_dir, &options).unwrap_err().stage,
            Stage::Proxy
        ));
        options.proxy = test_proxy();
        validate(&recipe_dir, &options).unwrap();

        for key in ["API_KEY", "MANAGEMENT_KEY"] {
            let mut missing = options.clone();
            missing.variables.remove(key);
            assert!(validate(&recipe_dir, &missing).is_err());
            for empty in ["", " \t\n"] {
                let mut invalid = options.clone();
                invalid.variables.insert(key.into(), empty.into());
                assert!(validate(&recipe_dir, &invalid).is_err());
            }
        }
        for port in [
            "", "0", "65536", "-1", "abc", "8.5", "08317", " 8317", "8317\n",
        ] {
            let mut invalid = options.clone();
            invalid.variables.insert("PORT".into(), port.into());
            assert!(
                validate(&recipe_dir, &invalid).is_err(),
                "accepted invalid port"
            );
        }
        for port in ["1", "65535"] {
            let mut valid = options.clone();
            valid.variables.insert("PORT".into(), port.into());
            validate(&recipe_dir, &valid).unwrap();
        }
        for removed in ["CONFIG_YAML", "PROXY_URL"] {
            let mut invalid = options.clone();
            invalid.variables.insert(removed.into(), "unused".into());
            assert!(validate(&recipe_dir, &invalid).is_err());
        }
        let mut default_port = options.clone();
        default_port.variables.remove("PORT");
        validate(&recipe_dir, &default_port).unwrap();

        let context = BTreeMap::from([("PIER_ARCH", architecture.as_str())]);
        let source = &recipe["source"];
        downloads.push(json!({
            "architecture": architecture.as_str(),
            "url": env.render_str(source["url"].as_str().unwrap(), &context).unwrap(),
            "sha256": env.render_str(source["sha256"].as_str().unwrap(), &context).unwrap(),
        }));
    }

    // Verify serialization, including text that must never become template or shell code.
    let mut values = variables();
    let unusual = "key:\"'\\\n# literal {{ 1 + 1 }} $(touch /tmp/pier-must-not-exist) 中文";
    values.insert("API_KEY".into(), unusual.into());
    values.insert("MANAGEMENT_KEY".into(), unusual.into());
    let template = fs::read_to_string(recipe_dir.join("configs/config.yaml")).unwrap();
    let rendered = env.render_str(&template, &values).unwrap();
    let config: Value = serde_yaml_ng::from_str(&rendered).unwrap();
    assert_eq!(config["access"]["api-keys"][0], unusual);
    assert_eq!(config["management"]["secret-key"], unusual);
    assert_eq!(config["server"]["host"], "127.0.0.1");
    assert_eq!(config["server"]["port"], 18317);

    // Resolve the actual blueprint mappings using Pier's template semantics.
    let blueprint = yaml(&root.join("blueprints/cliproxyapi/pier-blueprint.yml"));
    assert_eq!(blueprint["schema"], 1);
    let app = &blueprint["apps"][0];
    assert_eq!(app["app"], "apps/cliproxyapi");
    let mut supplied = variables();
    supplied.remove("PORT");
    for (key, declaration) in blueprint["variables"].as_object().unwrap() {
        if !supplied.contains_key(key) {
            supplied.insert(key.clone(), declaration["default"].as_str().unwrap().into());
        }
    }
    let mut options = PackOptions::new(Architecture::Amd64);
    options.proxy = test_proxy();
    for (key, mapping) in app["variables"].as_object().unwrap() {
        options.variables.insert(
            key.clone(),
            env.render_str(mapping.as_str().unwrap(), &supplied)
                .unwrap(),
        );
    }
    assert_eq!(options.variables["PORT"], "8317");
    validate(root.join(app["app"].as_str().unwrap()), &options).unwrap();
    json!({"downloads": downloads, "version": metadata.version})
}

fn package(root: &Path, arch: &str, base_url: &str, output: &Path) -> Value {
    let recipe_dir = root.join("apps/cliproxyapi");
    let temp = tempfile::tempdir().unwrap();
    let mut recipe = yaml(&recipe_dir.join("pier-pkg.yml"));
    let context = BTreeMap::from([("PIER_ARCH", arch)]);
    let original_url = environment()
        .render_str(recipe["source"]["url"].as_str().unwrap(), &context)
        .unwrap();
    let filename = original_url.rsplit('/').next().unwrap();
    // Serve the unchanged, checksum-pinned official bytes from a local cache.
    // Keep the production proxy switch; test_proxy() bypasses the local server.
    recipe["source"]["url"] = json!(format!("{base_url}/{filename}"));
    fs::write(
        temp.path().join("pier-pkg.yml"),
        serde_yaml_ng::to_string(&recipe).unwrap(),
    )
    .unwrap();
    for relative in ["configs/config.yaml", "scripts/start.sh"] {
        let target = temp.path().join(relative);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(recipe_dir.join(relative), target).unwrap();
    }
    let architecture: Architecture = arch.parse().unwrap();
    let mut options = PackOptions::new(architecture);
    options.variables = variables();
    options.proxy = test_proxy();
    if env::var_os("PIER_CHECK_NEXT_RELEASE").is_some() {
        options.variables.insert("PORT".into(), "18318".into());
        options
            .variables
            .insert("API_KEY".into(), "pier-check-client-next-df4013".into());
    }
    options.output_dir = output.to_path_buf();
    let artifact = pack(temp.path(), &options).unwrap();
    let destination = output.join("unpacked");
    let manifest = unpack(&artifact.path, &destination, &artifact.sha256, architecture).unwrap();
    assert_eq!(manifest.service.command, ["bin/start.sh"]);
    assert!(manifest.image.is_none());
    assert!(
        manifest
            .files
            .iter()
            .any(|f| f.path == Path::new("bin/cli-proxy-api")
                && f.elf.is_some()
                && f.mode == 0o755)
    );
    let config = yaml(&destination.join("configs/config.yaml"));
    assert_eq!(
        config["access"]["api-keys"][0],
        options.variables["API_KEY"]
    );
    assert_eq!(
        config["server"]["port"],
        options.variables["PORT"].parse::<u16>().unwrap()
    );
    // unpack() leaves the staging root private; the runtime test mounts it read-only
    // and runs as a different, unprivileged UID, like pier-agent does.
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&destination, fs::Permissions::from_mode(0o755)).unwrap();
    json!({"package": artifact.path, "unpacked": destination, "sha256": artifact.sha256})
}

fn main() {
    let args: Vec<_> = env::args().collect();
    let result = match args[1].as_str() {
        "check" => check(Path::new(&args[2])),
        "pack" => package(Path::new(&args[2]), &args[3], &args[4], Path::new(&args[5])),
        _ => panic!("unknown check mode"),
    };
    println!("{result}");
}
