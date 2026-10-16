use eventsource_stream::Eventsource;
use futures_util::{Stream, StreamExt};

use crate::client::ApiError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SseEvent {
    pub id: Option<String>,
    pub event: String,
    pub data: String,
}

pub fn decode_sse<S>(stream: S) -> impl Stream<Item = Result<SseEvent, ApiError>>
where
    S: Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Send + Unpin + 'static,
{
    stream.eventsource().map(|event| match event {
        Ok(event) => Ok(SseEvent {
            id: (!event.id.is_empty()).then_some(event.id),
            event: event.event,
            data: event.data,
        }),
        Err(error) => Err(ApiError::Stream(error.to_string())),
    })
}
