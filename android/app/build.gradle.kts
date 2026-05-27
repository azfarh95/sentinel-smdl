plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.azsentinel.smdliptv"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.azsentinel.smdliptv"
        minSdk = 26
        targetSdk = 34
        versionCode = 5
        versionName = "0.2.3"
    }

    // Pin debug signing to a committed-in-repo keystore so every CI /
    // dev / Docker build produces APKs with the same SHA — Android can
    // install updates in place. AGP's default debug.keystore lives in
    // ~/.android and is regenerated per machine / per container, which
    // produced "package conflict" errors when sideloading rebuilds.
    // Credentials are the Android-Studio convention (storepass=android,
    // keypass=android) and provide ZERO security — debug builds are
    // not for distribution. The keystore is committed to the repo on
    // purpose.
    signingConfigs {
        getByName("debug") {
            storeFile = rootProject.file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        getByName("debug") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
        getByName("release") {
            isMinifyEnabled = false
            // Use the debug keystore so we can produce a sideload-ready APK
            // without orchestrating a release key for this first cut.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.webkit:webkit:1.10.0")
}
