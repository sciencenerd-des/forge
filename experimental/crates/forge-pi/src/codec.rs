use std::io;

/// Stateful byte decoder for Pi's strict LF-delimited JSONL protocol.
/// It intentionally does not use a Unicode-aware line reader because U+2028
/// and U+2029 are valid characters inside JSON strings.
#[derive(Default)]
pub struct JsonlDecoder {
    buffered: Vec<u8>,
}

impl JsonlDecoder {
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<serde_json::Value>, io::Error> {
        self.buffered.extend_from_slice(bytes);
        let mut values = Vec::new();
        while let Some(index) = self.buffered.iter().position(|byte| *byte == b'\n') {
            let mut line: Vec<u8> = self.buffered.drain(..=index).collect();
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            if line.is_empty() {
                continue;
            }
            values.push(serde_json::from_slice(&line).map_err(io::Error::other)?);
        }
        Ok(values)
    }

    pub fn finish(mut self) -> Result<Option<serde_json::Value>, io::Error> {
        if self.buffered.last() == Some(&b'\r') {
            self.buffered.pop();
        }
        if self.buffered.is_empty() {
            return Ok(None);
        }
        serde_json::from_slice(&self.buffered)
            .map(Some)
            .map_err(io::Error::other)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_unicode_separators_inside_json() {
        let mut decoder = JsonlDecoder::default();
        let input = b"{\"message\":\"a\\u2028b\"}\r\n{\"type\":\"agent_settled\"}\n";
        let values = decoder.push(input).expect("decode");
        assert_eq!(values.len(), 2);
        assert_eq!(values[0]["message"], "a b");
    }

    #[test]
    fn handles_a_record_split_across_chunks() {
        let mut decoder = JsonlDecoder::default();
        assert!(
            decoder
                .push(b"{\"type\":\"mes")
                .expect("first chunk")
                .is_empty()
        );
        let values = decoder.push(b"sage_update\"}\n").expect("second chunk");
        assert_eq!(values[0]["type"], "message_update");
    }
}
