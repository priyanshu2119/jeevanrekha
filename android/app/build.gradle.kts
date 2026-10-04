plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.gms.google-services")
}

android {
    namespace = "org.jeevanrekha.responder"
    compileSdk = 36

    defaultConfig {
        applicationId = "org.jeevanrekha.responder"
        // minSdk 24: field workers carry low-end, older Android phones.
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"),
                          "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

dependencies {
    // Compose (BOM pins the individual artifact versions)
    implementation(platform("androidx.compose:compose-bom:2025.06.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")

    // Token storage in the Android Keystore-backed encrypted prefs
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // HTTP (polling + API); SSE not needed: FCM wakes the app, polling
    // covers the foreground.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // FCM: high-priority data messages are the only reliable way to wake a
    // killed/dozing device for an emergency dispatch alert.
    implementation(platform("com.google.firebase:firebase-bom:33.16.0"))
    implementation("com.google.firebase:firebase-messaging")
}
