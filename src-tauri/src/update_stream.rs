use crate::error::{AppError, AppResult};
use sha2::{Digest, Sha256};

pub(crate) struct VerifiedStream<'a> {
    verifier: minisign_verify::StreamVerifier<'a>,
    digest: Sha256,
}

impl<'a> VerifiedStream<'a> {
    pub(crate) fn new(
        key: &'a minisign_verify::PublicKey,
        signature: &'a minisign_verify::Signature,
    ) -> AppResult<Self> {
        let verifier = key.verify_stream(signature).map_err(|error| {
            AppError::Worker(format!("Unable to verify the update stream: {error}"))
        })?;
        Ok(Self {
            verifier,
            digest: Sha256::new(),
        })
    }

    pub(crate) fn update(&mut self, chunk: &[u8]) {
        self.verifier.update(chunk);
        self.digest.update(chunk);
    }

    pub(crate) fn finish(mut self) -> AppResult<String> {
        self.verifier.finalize().map_err(|error| {
            AppError::Worker(format!("Update signature verification failed: {error}"))
        })?;
        Ok(format!("{:x}", self.digest.finalize()))
    }
}
