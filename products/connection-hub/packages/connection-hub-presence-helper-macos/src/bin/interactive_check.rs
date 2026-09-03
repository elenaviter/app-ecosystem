fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let code = std::panic::catch_unwind(connection_hub_presence_helper::run_interactive_check)
        .unwrap_or_else(|_| {
            println!("FAIL: the interactive check could not complete.");
            1
        });
    std::process::exit(code);
}
