fn main() {
    std::process::exit(pymss_audio_host::run_cli().unwrap_or(2));
}
