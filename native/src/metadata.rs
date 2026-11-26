use std::{
    io::Read,
    time::{Duration, SystemTime},
};

use anyhow::Result;
use flate2::read::MultiGzDecoder;
use reqwest::{
    Response, StatusCode, Url,
    header::{self, HeaderMap, HeaderValue},
};
use serde_json::{Map, Value, json};

const MAX_BODY: usize = 8 * 1024 * 1024;
const MAX_ERROR: usize = 16 * 1024;

pub struct Client(reqwest::Client);

pub enum Fetch {
    Unmodified,
    Data(Value, Map<String, Value>),
}

pub enum Failure {
    Invalid,
    Rejected(Rejection),
}

pub struct Rejection {
    status: u16,
    category: &'static str,
    attempts: usize,
    retry_after: Option<f64>,
}

impl Failure {
    pub fn diagnostic(&self) -> Option<Value> {
        let Self::Rejected(failure) = self else {
            return None;
        };
        let mut value = json!({"httpStatus":failure.status,"category":failure.category,"attempts":failure.attempts});
        if let Some(seconds) = failure.retry_after {
            value["retryAfterSeconds"] = json!(seconds);
        }
        Some(value)
    }

    pub fn cooldown(&self) -> Option<f64> {
        let Self::Rejected(failure) = self else {
            return None;
        };
        if matches!(failure.status, 429 | 503) && failure.category == "temporarily-unavailable" {
            failure.retry_after.filter(|seconds| *seconds > 0.0)
        } else {
            None
        }
    }
}

fn authorized_url(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
}

async fn bounded_body(response: &mut Response, limit: usize) -> Option<Vec<u8>> {
    let declared = match response.headers().get(header::CONTENT_LENGTH) {
        Some(value) => Some(
            value
                .to_str()
                .ok()?
                .parse::<usize>()
                .ok()
                .filter(|size| *size <= limit)?,
        ),
        None => None,
    };
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if body.len() + chunk.len() > limit {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    declared
        .is_none_or(|size| size == body.len())
        .then_some(body)
}

fn retry_after(headers: &HeaderMap) -> Result<Option<f64>, ()> {
    let Some(value) = headers.get(header::RETRY_AFTER) else {
        return Ok(None);
    };
    let value = value
        .to_str()
        .ok()
        .filter(|value| value.len() <= 128)
        .ok_or(())?
        .trim();
    if !value.is_empty() && value.len() <= 10 && value.bytes().all(|byte| byte.is_ascii_digit()) {
        return value
            .parse::<u64>()
            .map(|seconds| Some(seconds as f64))
            .map_err(|_| ());
    }
    httpdate::parse_http_date(value)
        .map(|date| {
            Some(
                date.duration_since(SystemTime::now())
                    .unwrap_or_default()
                    .as_secs_f64(),
            )
        })
        .map_err(|_| ())
}

fn classification(status: u16, body: Option<&[u8]>) -> (&'static str, bool) {
    let value = body.and_then(|body| serde_json::from_slice::<Value>(body).ok());
    let complete = body.is_some_and(|body| body.is_empty() || value.is_some());
    let detail = value
        .as_ref()
        .map(|value| value.get("error").unwrap_or(value));
    let codes: Vec<&str> = detail
        .into_iter()
        .flat_map(|detail| {
            ["code", "type"]
                .into_iter()
                .filter_map(|key| detail[key].as_str())
        })
        .collect();
    if matches!(status, 401 | 403)
        || codes.iter().any(|code| {
            matches!(
                *code,
                "invalid_api_key"
                    | "invalid_token"
                    | "invalid_credential"
                    | "invalid_credentials"
                    | "authentication_error"
                    | "authentication_failed"
                    | "unauthorized"
                    | "permission_denied"
                    | "permission_error"
                    | "access_denied"
                    | "insufficient_quota"
                    | "billing_error"
            )
        })
    {
        ("credential-or-permission", false)
    } else if codes.iter().any(|code| {
        matches!(
            *code,
            "unsupported_parameter" | "unknown_parameter" | "invalid_parameter"
        )
    }) {
        ("unsupported-parameter", false)
    } else if matches!(status, 400 | 429 | 503)
        && codes.iter().any(|code| {
            matches!(
                *code,
                "rate_limit_exceeded"
                    | "rate_limit_error"
                    | "overloaded_error"
                    | "server_overloaded"
                    | "temporarily_unavailable"
                    | "service_unavailable"
            )
        })
    {
        ("temporarily-unavailable", complete)
    } else if codes.iter().any(|code| {
        matches!(
            *code,
            "invalid_request_error" | "invalid_request" | "bad_request"
        )
    }) {
        ("invalid-request", false)
    } else if matches!(status, 429 | 503) && codes.is_empty() {
        ("temporarily-unavailable", complete)
    } else {
        (
            if status == 400 {
                "invalid-request"
            } else {
                "request-rejected"
            },
            false,
        )
    }
}

fn json_content_type(headers: &HeaderMap) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|kind| {
            let kind = kind.trim().to_ascii_lowercase();
            kind == "application/json" || kind.ends_with("+json")
        })
}

async fn metadata(mut response: Response) -> Result<Fetch, Failure> {
    if !json_content_type(response.headers()) {
        return Err(Failure::Invalid);
    }
    let encoding = response
        .headers()
        .get(header::CONTENT_ENCODING)
        .map(HeaderValue::to_str)
        .transpose()
        .map_err(|_| Failure::Invalid)?
        .unwrap_or("identity")
        .to_ascii_lowercase();
    let validators = [
        ("ETag", header::ETAG),
        ("Last-Modified", header::LAST_MODIFIED),
    ]
    .into_iter()
    .filter_map(|(name, key)| {
        response
            .headers()
            .get(key)
            .and_then(|value| value.to_str().ok())
            .filter(|value| {
                !value.is_empty() && value.len() <= 4096 && !value.contains(['\r', '\n', '\0'])
            })
            .map(|value| (name.into(), json!(value)))
    })
    .collect();
    let mut body = bounded_body(&mut response, MAX_BODY)
        .await
        .ok_or(Failure::Invalid)?;
    if encoding == "gzip" {
        let mut decoded = Vec::new();
        MultiGzDecoder::new(body.as_slice())
            .take(MAX_BODY as u64 + 1)
            .read_to_end(&mut decoded)
            .map_err(|_| Failure::Invalid)?;
        if decoded.len() > MAX_BODY {
            return Err(Failure::Invalid);
        }
        body = decoded;
    } else if !matches!(encoding.as_str(), "" | "identity") {
        return Err(Failure::Invalid);
    }
    let value = serde_json::from_slice(&body).map_err(|_| Failure::Invalid)?;
    Ok(Fetch::Data(value, validators))
}

impl Client {
    pub fn new() -> Result<Self> {
        let redirect = reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= 10
                || !authorized_url(attempt.url())
                || !attempt
                    .previous()
                    .first()
                    .is_some_and(|first| first.origin() == attempt.url().origin())
            {
                attempt.error("metadata redirect authority differs")
            } else {
                attempt.follow()
            }
        });
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(redirect)
            .retry(reqwest::retry::never())
            .user_agent(concat!("ModelHub-Kit/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self(client))
    }

    pub async fn get(&self, url: &str, mut headers: HeaderMap) -> Result<Fetch, Failure> {
        if url.len() > 16_384 || url.chars().any(char::is_control) {
            return Err(Failure::Invalid);
        }
        let url = Url::parse(url)
            .ok()
            .filter(authorized_url)
            .ok_or(Failure::Invalid)?;
        headers.insert(header::ACCEPT, HeaderValue::from_static("application/json"));
        headers.insert(
            header::ACCEPT_ENCODING,
            HeaderValue::from_static("identity"),
        );
        let delays = [0.25_f64, 0.5];
        for attempt in 0..=delays.len() {
            let mut response = self
                .0
                .get(url.clone())
                .headers(headers.clone())
                .send()
                .await
                .map_err(|_| Failure::Invalid)?;
            let status = response.status();
            if status == StatusCode::NOT_MODIFIED {
                return if headers.contains_key(header::IF_NONE_MATCH)
                    || headers.contains_key(header::IF_MODIFIED_SINCE)
                {
                    Ok(Fetch::Unmodified)
                } else {
                    Err(Failure::Invalid)
                };
            }
            if status == StatusCode::OK {
                return metadata(response).await;
            }
            let retry = retry_after(response.headers());
            let body = bounded_body(&mut response, MAX_ERROR).await;
            let (category, transient) = classification(status.as_u16(), body.as_deref());
            if !transient
                || attempt == delays.len()
                || retry.is_err()
                || retry.is_ok_and(|value| value.is_some_and(|value| value > 5.0))
            {
                return Err(Failure::Rejected(Rejection {
                    status: status.as_u16(),
                    category,
                    attempts: attempt + 1,
                    retry_after: retry.ok().flatten(),
                }));
            }
            tokio::time::sleep(Duration::from_secs_f64(
                delays[attempt].max(retry.ok().flatten().unwrap_or_default()),
            ))
            .await;
        }
        unreachable!("the final metadata response always returns")
    }
}
