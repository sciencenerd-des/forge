use std::{env, fs, path::PathBuf};

use anyhow::{Context, Result, anyhow};
use serde_json::{Value, json};

fn store_path() -> Result<PathBuf> {
    let home = env::var_os("FORGE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".forge")))
        .ok_or_else(|| anyhow!("FORGE_HOME or HOME is not set"))?;
    Ok(home.join("providers.json"))
}

pub fn save_profile(
    role: &str,
    base_url: &str,
    model: &str,
    api_key: &str,
    auth_mode: &str,
) -> Result<PathBuf> {
    if role.is_empty() || base_url.is_empty() || model.is_empty() {
        return Err(anyhow!("role, base_url, and model are required"));
    }
    let path = store_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut document: Value = if path.exists() {
        serde_json::from_slice(&fs::read(&path).context("read providers.json")?)?
    } else {
        json!({"version": 1, "profiles": {}})
    };
    if document.get("version") != Some(&json!(1)) {
        return Err(anyhow!("providers.json version must be 1"));
    }
    document["profiles"][role] =
        json!({"base_url": base_url, "model": model, "api_key": api_key, "auth_mode": auth_mode});
    let temporary = path.with_extension("json.tmp");
    fs::write(&temporary, serde_json::to_vec_pretty(&document)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    }
    fs::rename(&temporary, &path)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn writes_shared_provider_shape_with_restrictive_permissions() {
        let directory = tempfile::tempdir().unwrap();
        unsafe {
            env::set_var("FORGE_HOME", directory.path());
        }
        let path = save_profile(
            "executor",
            "http://localhost:1234/v1",
            "model",
            "secret",
            "api_key",
        )
        .unwrap();
        let value: Value = serde_json::from_slice(&fs::read(path.clone()).unwrap()).unwrap();
        assert_eq!(value["version"], 1);
        assert_eq!(value["profiles"]["executor"]["model"], "model");
        #[cfg(unix)]
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}
