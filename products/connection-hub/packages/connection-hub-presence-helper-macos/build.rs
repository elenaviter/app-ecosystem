fn main() {
    println!("cargo:rerun-if-env-changed=KDCUBE_HELPER_VERSION");
    println!("cargo:rerun-if-env-changed=KDCUBE_KEYCHAIN_ACCESS_GROUP");
    emit("KDCUBE_HELPER_VERSION", "development");
    emit("KDCUBE_KEYCHAIN_ACCESS_GROUP", "");
}

fn emit(name: &str, default: &str) {
    let value = std::env::var(name).unwrap_or_else(|_| default.to_owned());
    assert!(
        value.len() <= 256 && !value.chars().any(char::is_control),
        "invalid compile-time helper metadata"
    );
    println!("cargo:rustc-env={name}={value}");
}
