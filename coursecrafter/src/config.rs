//! Optional user config: `%APPDATA%\piscinebuilder\config.yaml` on Windows,
//! `$XDG_CONFIG_HOME/piscinebuilder/config.yaml` or `~/.config/piscinebuilder/config.yaml`
//! elsewhere. Holds choices, never secrets: keys stay in the environment.

use serde::Deserialize;
use std::path::PathBuf;

#[derive(Deserialize, Default, Debug)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub provider: Option<String>,
    pub base_url: Option<String>,
    pub model: Option<String>,
    /// Name of the environment variable holding the key.
    pub api_key_env: Option<String>,
}

pub fn path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("PISCINEBUILDER_CONFIG") {
        return Some(PathBuf::from(p));
    }
    let base = if cfg!(windows) {
        std::env::var_os("APPDATA").map(PathBuf::from)
    } else {
        std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
    }?;
    Some(base.join("piscinebuilder").join("config.yaml"))
}

pub fn load() -> Result<Config, String> {
    let Some(p) = path() else {
        return Ok(Config::default());
    };
    match std::fs::read_to_string(&p) {
        Ok(text) => yaml_serde::from_str(&text).map_err(|e| format!("{}: {e}", p.display())),
        Err(_) => Ok(Config::default()),
    }
}
