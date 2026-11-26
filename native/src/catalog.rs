use std::{
    collections::{BTreeMap, HashSet},
    env,
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{Result, ensure};
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderValue, IF_MODIFIED_SINCE, IF_NONE_MATCH};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

use crate::metadata::{Client, Fetch};

const LIMITS: [(&str, Option<&str>, Option<&str>); 5] = [
    ("context_window", Some("contextWindow"), Some("context")),
    ("max_input_tokens", Some("maxInputTokens"), Some("input")),
    ("max_output_tokens", Some("maxOutputTokens"), Some("output")),
    ("max_context_window", Some("maxContextWindow"), None),
    ("auto_compact_token_limit", None, None),
];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    id: String,
    url: Option<String>,
    bearer_env: Option<String>,
    provider_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Operation {
    Sync,
    Limits,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub operation: Operation,
    pub prior_bytes: usize,
    source: Source,
    cooldown: Option<Value>,
}

#[derive(Serialize)]
pub struct Reply {
    result: Value,
    record: Option<Value>,
    cooldown: Option<Value>,
}

fn safe_id(value: &str, max: usize) -> bool {
    let mut bytes = value.bytes();
    let allowed = |c: u8| c.is_ascii_lowercase() || c.is_ascii_digit();
    value.len() <= max
        && bytes.next().is_some_and(allowed)
        && bytes.all(|c| allowed(c) || matches!(c, b'_' | b'-'))
}

fn environment_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 128
        && bytes.next().is_some_and(|c| c.is_ascii_uppercase())
        && bytes.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_')
}

// Match the installed cache's sorted, ASCII JSON scope encoding exactly.
fn scope_json(value: &str) -> String {
    use std::fmt::Write;
    let encoded = serde_json::to_string(value).expect("a JSON string is serializable");
    let mut result = String::with_capacity(encoded.len());
    for ch in encoded.chars() {
        if ch < '\u{7f}' {
            result.push(ch);
        } else {
            let mut units = [0_u16; 2];
            for unit in ch.encode_utf16(&mut units) {
                write!(result, "\\u{unit:04x}").unwrap();
            }
        }
    }
    result
}

impl Source {
    fn scope(&self, credential: Option<&str>) -> String {
        let mut scope: BTreeMap<&str, String> = BTreeMap::from([
            (
                "url",
                self.url
                    .as_deref()
                    .map_or_else(|| "null".into(), scope_json),
            ),
            (
                "bearer_env",
                self.bearer_env
                    .as_deref()
                    .map_or_else(|| "null".into(), scope_json),
            ),
        ]);
        if let Some(provider) = &self.provider_id {
            scope.insert("provider_id", scope_json(provider));
        }
        if let Some(credential) = credential {
            let mut digest = Sha256::new();
            digest.update(b"ModelHub-Kit catalog credential scope\0");
            digest.update(credential);
            scope.insert("authScope", scope_json(&hex::encode(digest.finalize())));
        }
        let entries: Vec<_> = scope
            .into_iter()
            .map(|(key, value)| format!("\"{key}\": {value}"))
            .collect();
        hex::encode(Sha256::digest(format!("{{{}}}", entries.join(", "))))
    }

    fn credential(&self) -> Result<Option<String>> {
        let Some(name) = self.bearer_env.as_deref().filter(|name| !name.is_empty()) else {
            return Ok(None);
        };
        ensure!(
            environment_name(name),
            "credential-environment-name-required"
        );
        let credential = env::var(name).ok().filter(|value| !value.is_empty());
        Ok(credential)
    }
}

fn budgets(row: &Value) -> Result<Map<String, Value>> {
    let containers: Vec<_> = ["limit", "limits"]
        .into_iter()
        .filter_map(|key| row.get(key))
        .filter(|value| !value.is_null())
        .collect();
    ensure!(
        containers.iter().all(|item| item.is_object()),
        "object-model-token-limits-required"
    );
    let mut output = Map::new();
    for (name, alias, nested) in LIMITS {
        let at_root = [row.get(name), alias.and_then(|alt| row.get(alt))];
        let declarations = at_root.into_iter().flatten().chain(
            containers
                .iter()
                .filter_map(|item| nested.and_then(|key| item.get(key))),
        );
        let mut known = None;
        for value in declarations.filter(|value| !value.is_null()) {
            let amount = value
                .as_u64()
                .filter(|amount| *amount > 0)
                .ok_or_else(|| anyhow::anyhow!("positive-model-token-metadata-required"))?;
            ensure!(
                known.is_none_or(|first| first == amount),
                "conflicting-model-token-metadata"
            );
            known = Some(amount);
        }
        if let Some(amount) = known {
            output.insert(name.into(), json!(amount));
        }
    }
    if let Some(percent) = row
        .get("effective_context_window_percent")
        .filter(|value| !value.is_null())
    {
        ensure!(
            percent
                .as_u64()
                .is_some_and(|value| (1..=100).contains(&value)),
            "invalid-effective-context-percent"
        );
        output.insert("effective_context_window_percent".into(), percent.clone());
    }
    Ok(output)
}

fn model_rows(rows: &[Value]) -> Result<Vec<Value>> {
    ensure!(
        !rows.is_empty() && rows.len() <= 20_000,
        "bounded-model-array-required"
    );
    let mut seen = HashSet::with_capacity(rows.len());
    rows.iter()
        .map(|row| {
            let fields = row
                .as_object()
                .ok_or_else(|| anyhow::anyhow!("invalid-model-record"))?;
            let id = fields
                .get("id")
                .or_else(|| fields.get("slug"))
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty() && id.chars().count() <= 512 && seen.insert(*id))
                .ok_or_else(|| anyhow::anyhow!("unique-exact-model-ids-required"))?;
            let mut item = Map::new();
            item.insert("id".into(), json!(id));
            for (key, value) in fields {
                let lowered = key.to_lowercase();
                if matches!(
                    key.as_str(),
                    "name"
                        | "display_name"
                        | "effective_context_window_percent"
                        | "limit"
                        | "limits"
                        | "input_modalities"
                        | "output_modalities"
                        | "capabilities"
                        | "variants"
                        | "parameters"
                ) || LIMITS.iter().any(|&(name, _, _)| name == key)
                    || ["reason", "effort", "think"]
                        .iter()
                        .any(|term| lowered.contains(term))
                {
                    item.insert(key.clone(), value.clone());
                }
            }
            item.extend(budgets(row)?);
            Ok(Value::Object(item))
        })
        .collect()
}

fn source_rows(value: &Value, source: &Source) -> Result<Vec<Value>> {
    let Some(id) = &source.provider_id else {
        let rows = value
            .get("data")
            .or_else(|| value.get("models"))
            .and_then(Value::as_array)
            .ok_or_else(|| anyhow::anyhow!("model-array-required"))?;
        return model_rows(rows);
    };
    let provider = &value.get("providers").unwrap_or(value)[id];
    let rows = provider["models"]
        .as_object()
        .filter(|rows| rows.len() <= 20_000)
        .ok_or_else(|| anyhow::anyhow!("exact-catalog-provider-missing-or-invalid"))?;
    ensure!(
        provider.get("id").is_none_or(|value| value == id),
        "exact-catalog-provider-id-required"
    );
    let mut array = Vec::with_capacity(rows.len());
    for (id, row) in rows {
        let mut row = row
            .as_object()
            .filter(|row| row.get("id").is_none_or(|value| value == id))
            .ok_or_else(|| anyhow::anyhow!("catalog-model-key-disagreement"))?
            .clone();
        row.insert("id".into(), json!(id));
        for key in ["limit", "limits"] {
            if let Some(footer) = row.get_mut(key).and_then(Value::as_object_mut) {
                for key in ["context", "input", "output"] {
                    if let Some(value) = footer
                        .get_mut(key)
                        .filter(|value| value.as_u64() == Some(0))
                    {
                        *value = Value::Null;
                    }
                }
            }
        }
        array.push(Value::Object(row));
    }
    model_rows(&array)
}

fn unix_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

impl Request {
    pub async fn process(self, client: &mut Option<Client>, prior: Option<Value>) -> Result<Reply> {
        let Self {
            operation,
            source,
            cooldown,
            ..
        } = self;
        ensure!(safe_id(&source.id, 64), "unique-safe-source-id-required");
        ensure!(
            source
                .provider_id
                .as_deref()
                .is_none_or(|id| safe_id(id, 128)),
            "exact-catalog-provider-id-required"
        );
        let credential = source.credential()?;
        let missing = source
            .bearer_env
            .as_ref()
            .is_some_and(|name| !name.is_empty())
            && credential.is_none();
        let fingerprint = source.scope(credential.as_deref());
        let prior = prior.and_then(|cached| {
            let rows = model_rows(cached["models"].as_array()?).ok()?;
            Some((cached, rows))
        });
        let scoped = prior
            .as_ref()
            .filter(|(cached, _)| !missing && cached["sourceFingerprint"] == fingerprint);
        if matches!(operation, Operation::Limits) {
            let mut result = json!({"source":source.id,"provider_id":source.provider_id,"status":"queued-metadata-unavailable","models":[]});
            if let Some((cached, rows)) = scoped {
                let mut report = Vec::with_capacity(rows.len());
                for row in rows {
                    let budget = budgets(row)?;
                    let active = ["context_window", "max_context_window"]
                        .into_iter()
                        .filter_map(|name| budget.get(name).and_then(Value::as_u64))
                        .max();
                    let mut observed = budget;
                    observed.insert("id".into(), row["id"].clone());
                    observed.insert(
                        "status".into(),
                        json!(if active.is_some() {
                            "declared"
                        } else {
                            "unknown"
                        }),
                    );
                    observed.insert("active_context_window".into(), json!(active));
                    report.push(Value::Object(observed));
                }
                result["status"] = json!("ready");
                result["models"] = json!(report);
                result["observedAt"] = cached["observedAt"].clone();
            }
            return Ok(Reply {
                result,
                record: None,
                cooldown: None,
            });
        }

        let accepted = |cached: &Value| {
            cached["validators"].as_object().is_some_and(|items| {
                items.iter().all(|(key, value)| {
                    matches!(key.as_str(), "ETag" | "Last-Modified")
                        && value.as_str().is_some_and(|text| {
                            text.len() <= 4096 && HeaderValue::from_str(text).is_ok()
                        })
                })
            })
        };
        if missing {
            let result = json!({"source":source.id,"status":"missing-credential-reference","retained":prior.as_ref().is_some_and(|(cached, _)| accepted(cached))});
            return Ok(Reply {
                result,
                record: None,
                cooldown,
            });
        }
        let scoped = scoped
            .map(|(cached, _)| cached)
            .filter(|cached| accepted(cached));
        let now = unix_seconds();
        let keep_cooldown = cooldown.filter(|entry| {
            entry["fingerprint"] == fingerprint
                && entry["until"]
                    .as_u64()
                    .is_some_and(|until| until as f64 > now)
        });
        if let Some(entry) = keep_cooldown {
            let seconds = (entry["until"].as_u64().unwrap() as f64 - now).ceil() as u64;
            let result = json!({"source":source.id,"status":"queued-rate-limit","retained":scoped.is_some(),"retryAfterSeconds":seconds});
            return Ok(Reply {
                result,
                record: None,
                cooldown: Some(entry),
            });
        }
        let mut headers = HeaderMap::new();
        if let Some(credential) = credential {
            let mut value = HeaderValue::from_str(&format!("Bearer {credential}"))?;
            value.set_sensitive(true);
            headers.insert(AUTHORIZATION, value);
        }
        if let Some(cached) = scoped {
            for (key, header) in [
                ("ETag", IF_NONE_MATCH),
                ("Last-Modified", IF_MODIFIED_SINCE),
            ] {
                if let Some(value) = cached["validators"][key].as_str() {
                    headers.insert(header, HeaderValue::from_str(value)?);
                }
            }
        }
        let mut record = None;
        let mut cooldown = None;
        let mut result = json!({"source":source.id,"status":if scoped.is_some(){"refresh-failed-last-good-retained"}else{"refresh-failed"},"retained":scoped.is_some()});
        if client.is_none() {
            *client = Client::new().ok();
        }
        let Some(client) = client else {
            return Ok(Reply {
                result,
                record,
                cooldown,
            });
        };
        match client
            .get(source.url.as_deref().unwrap_or_default(), headers)
            .await
        {
            Ok(Fetch::Unmodified) if scoped.is_some() => {
                let rows = scoped
                    .and_then(|cached| cached["models"].as_array())
                    .unwrap();
                result = json!({"source":source.id,"status":"not-modified","models":rows.len(),"changed":false});
            }
            Ok(Fetch::Data(value, validators)) => {
                if let Ok(models) = source_rows(&value, &source) {
                    let count = models.len();
                    let mut current = Map::from_iter([
                        ("version".into(), json!(1)),
                        ("sourceFingerprint".into(), json!(fingerprint)),
                        ("models".into(), Value::Array(models)),
                        ("validators".into(), Value::Object(validators)),
                    ]);
                    let changed = scoped.is_none_or(|cached| cached["models"] != current["models"]);
                    if scoped.is_none_or(|cached| {
                        cached.as_object().is_none_or(|value| {
                            value
                                .iter()
                                .filter(|(key, _)| key.as_str() != "observedAt")
                                .count()
                                != current.len()
                                || current
                                    .iter()
                                    .any(|(key, value)| cached.get(key) != Some(value))
                        })
                    }) {
                        current.insert(
                            "observedAt".into(),
                            json!(OffsetDateTime::now_utc().format(&Rfc3339)?),
                        );
                        record = Some(Value::Object(current));
                    }
                    result = json!({"source":source.id,"status":"ready","models":count,"changed":changed,"written":false});
                }
            }
            Err(failure) => {
                if let Some(diagnostic) = failure.diagnostic() {
                    result["diagnostic"] = diagnostic;
                    if let Some(seconds) = failure.cooldown() {
                        cooldown = Some(
                            json!({"fingerprint":fingerprint,"until":(unix_seconds()+seconds).ceil() as u64}),
                        );
                        result["status"] = json!("queued-rate-limit");
                        result["retryAfterSeconds"] = json!(seconds.ceil() as u64);
                    }
                }
            }
            _ => {}
        }
        Ok(Reply {
            result,
            record,
            cooldown,
        })
    }
}
