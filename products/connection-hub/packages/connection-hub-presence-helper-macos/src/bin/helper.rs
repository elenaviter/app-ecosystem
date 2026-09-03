fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let result = std::panic::catch_unwind(connection_hub_presence_helper::run_helper).unwrap_or(1);
    std::process::exit(result);
}
