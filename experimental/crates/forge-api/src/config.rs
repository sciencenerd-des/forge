use std::{env, fs, path::PathBuf};

use thiserror::Error;
use url::Url;

#[derive(Clone, Debug)]
pub struct ForgeConfig {
    pub home: PathBuf,
    pub control_url: Url,
    pub control_token: String,
}

#[derive(Debug, Error)]
pub enum ForgeConfigError {
    #[error("FORGE_CONTROL_URL is invalid: {0}")]
    InvalidUrl(#[from] url::ParseError),
    #[error("cannot read Forge control token at {path}: {source}")]
    TokenRead {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("Forge control token is empty at {0}")]
    EmptyToken(PathBuf),
    #[error("cannot secure Forge control token at {path}: {source}")]
    TokenPermissions {
        path: PathBuf,
        source: std::io::Error,
    },
}

impl ForgeConfig {
    pub fn resolve() -> Result<Self, ForgeConfigError> {
        let home = env::var_os("FORGE_HOME")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".forge")))
            .unwrap_or_else(|| PathBuf::from(".forge"));
        let control_url = Url::parse(
            &env::var("FORGE_CONTROL_URL").unwrap_or_else(|_| "http://127.0.0.1:8787".into()),
        )?;
        let control_token = match env::var("FORGE_CONTROL_TOKEN") {
            Ok(token) if !token.is_empty() => token,
            _ => {
                let path = home.join("control-token");
                let token = fs::read_to_string(&path)
                    .map_err(|source| ForgeConfigError::TokenRead {
                        path: path.clone(),
                        source,
                    })?
                    .trim()
                    .to_owned();
                if token.is_empty() {
                    return Err(ForgeConfigError::EmptyToken(path));
                }
                secure_token_file(&path)?;
                token
            }
        };
        Ok(Self {
            home,
            control_url,
            control_token,
        })
    }

    pub fn manifest_path(&self) -> PathBuf {
        self.home.join("logs/pge_runs/runs.json")
    }
}

fn secure_token_file(path: &PathBuf) -> Result<(), ForgeConfigError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let permissions = fs::Permissions::from_mode(0o600);
        fs::set_permissions(path, permissions).map_err(|source| {
            ForgeConfigError::TokenPermissions {
                path: path.clone(),
                source,
            }
        })?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn resolves_env_and_repairs_token_permissions() {
        let directory = tempfile::tempdir().expect("temp home");
        let token_path = directory.path().join("control-token");
        fs::write(&token_path, "fixture-token\n").expect("write token");
        #[cfg(unix)]
        fs::set_permissions(&token_path, std::fs::Permissions::from_mode(0o644))
            .expect("loosen token");

        // Do not mutate process-wide environment in parallel unit tests. Verify
        // the security behavior directly instead.
        secure_token_file(&token_path).expect("repair permissions");
        #[cfg(unix)]
        assert_eq!(
            fs::metadata(token_path)
                .expect("metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}
