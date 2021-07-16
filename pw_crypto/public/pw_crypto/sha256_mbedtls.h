// Copyright 2021 The Pigweed Authors
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not
// use this file except in compliance with the License. You may obtain a copy of
// the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations under
// the License.

#pragma once

#include "mbedtls/sha256.h"

namespace pw::crypto::sha256::backend {

enum Sha256State {
  // Successfully initialized (during contruction).
  kInitialized,
  // Finalized as a result of the first Final() call.
  kFinalized,
  // Invalid/unrecoverable state.
  kError,
};

struct Sha256Context {
  Sha256State state;
  mbedtls_sha256_context native_context;
};

}  // namespace pw::crypto::sha256::backend
