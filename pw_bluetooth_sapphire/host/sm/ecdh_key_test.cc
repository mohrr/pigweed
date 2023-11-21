// Copyright 2023 The Pigweed Authors
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

#include "pw_bluetooth_sapphire/internal/host/sm/ecdh_key.h"

#include <gtest/gtest.h>

#include <cstring>
#include <memory>

#include "pw_bluetooth_sapphire/internal/host/common/uint256.h"

namespace bt::sm {
namespace {

TEST(EcdhKeyTest, ParseSerializedKey) {
  // Debug ECDH key given in V5.1 Vol. 3 Part H Section 2.3.5.6.1
  const UInt256 kDebugPubKeyX{0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC,
                              0xDB, 0xFD, 0xF4, 0xAC, 0x11, 0x91, 0xF4, 0xEF,
                              0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83, 0x2C, 0x5E,
                              0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x20};
  const UInt256 kDebugPubKeyY{0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74,
                              0x24, 0x30, 0xED, 0x8F, 0xC2, 0x45, 0x63, 0x76,
                              0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A, 0x32, 0x63,
                              0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0xDC};
  // Debug ECDH key given in V5.1 Vol. 3 Part H Section 2.3.5.6.1, converted to
  // little-endian to match transport format.
  const sm::PairingPublicKeyParams kSerializedKey{
      .x = {0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC, 0xDB, 0xFD, 0xF4,
            0xAC, 0x11, 0x91, 0xF4, 0xEF, 0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83,
            0x2C, 0x5E, 0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x20},
      .y = {0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74, 0x24, 0x30, 0xED,
            0x8F, 0xC2, 0x45, 0x63, 0x76, 0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A,
            0x32, 0x63, 0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0xDC}};
  auto new_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  ASSERT_TRUE(new_key.has_value());
  ASSERT_EQ(kDebugPubKeyX, new_key->GetPublicKeyX());
  ASSERT_EQ(kDebugPubKeyY, new_key->GetPublicKeyY());
}

TEST(EcdhKeyTest, PointOffP256CurveXValueParsesToNullopt) {
  // These values come from the debug ECDH key in V5.1 Vol. 3 Part H
  // Section 2.3.5.6.1 (converted to little-endian). The debug ECDH key values
  // are on the P-256 curve, but by changing only the X-coordinate's
  // most-significant byte from 0x20 to 0x00, we create a point off the P-256
  // curve.
  const sm::PairingPublicKeyParams kSerializedKey{
      .x = {0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC, 0xDB, 0xFD, 0xF4,
            0xAC, 0x11, 0x91, 0xF4, 0xEF, 0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83,
            0x2C, 0x5E, 0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x00},
      .y = {0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74, 0x24, 0x30, 0xED,
            0x8F, 0xC2, 0x45, 0x63, 0x76, 0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A,
            0x32, 0x63, 0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0xDC}};
  auto new_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  ASSERT_EQ(new_key, std::nullopt);
}

TEST(EcdhKeyTest, PointOffP256CurveYValueParsesToNullopt) {
  // These values come from the debug ECDH key in V5.1 Vol. 3 Part H
  // Section 2.3.5.6.1 (converted to little-endian). The debug ECDH key values
  // are on the P-256 curve, but by changing only the Y-coordinate's
  // most-significant byte from 0xDC to 0x00, we create a point off the P-256
  // curve.
  const sm::PairingPublicKeyParams kSerializedKey{
      .x = {0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC, 0xDB, 0xFD, 0xF4,
            0xAC, 0x11, 0x91, 0xF4, 0xEF, 0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83,
            0x2C, 0x5E, 0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x20},
      .y = {0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74, 0x24, 0x30, 0xED,
            0x8F, 0xC2, 0x45, 0x63, 0x76, 0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A,
            0x32, 0x63, 0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0x00}};
  auto new_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  ASSERT_EQ(new_key, std::nullopt);
}

TEST(EcdhKeyTest, CreateGivesValidKey) {
  std::optional<LocalEcdhKey> new_key = LocalEcdhKey::Create();
  ASSERT_TRUE(new_key.has_value());
  auto serialized_pub_key = new_key->GetSerializedPublicKey();
  std::optional<EcdhKey> parsed_key =
      EcdhKey::ParseFromPublicKey(serialized_pub_key);
  ASSERT_TRUE(parsed_key.has_value());
}

// Test vector taken from NIST ECDH P-256 test vector 0 in first link, described
// in second link:
// https://csrc.nist.gov/CSRC/media/Projects/Cryptographic-Algorithm-Validation-Program/documents/components/ecccdhtestvectors.zip
// Described here:
// https://csrc.nist.gov/CSRC/media/Projects/Cryptographic-Algorithm-Validation-Program/documents/components/ecccdhvs.pdf
// Local private key is dIUT value, Peer X and Y are taken from QCAVSx and
// QCAVSy, ExpectedDHKey is ZIUT. The examples are given in human-readable
// big-endian format, but here we've converted them to little-endian format for
// consistency with the bt-host stack.
TEST(EcdhKeyTest, CalculateDhKeyWorks) {
  std::optional<LocalEcdhKey> local_key = LocalEcdhKey::Create();
  ASSERT_TRUE(local_key.has_value());
  const UInt256 kSamplePrivateKey{
      0x34, 0xA5, 0xC1, 0x2B, 0xB6, 0xAD, 0x0B, 0xD8, 0x2E, 0xD2, 0xB6,
      0x1F, 0xAF, 0x58, 0x90, 0x3D, 0xE0, 0xEA, 0x2E, 0x63, 0x14, 0x62,
      0x0D, 0xF8, 0xDA, 0x9D, 0xB2, 0x1E, 0xF7, 0xC5, 0x7D, 0x7D};
  local_key->SetPrivateKeyForTesting(kSamplePrivateKey);

  const sm::PairingPublicKeyParams kSerializedKey{
      .x = {0x87, 0xD2, 0x33, 0x88, 0x83, 0xCC, 0xE7, 0x2C, 0xB4, 0xF6, 0x4D,
            0x3A, 0xCE, 0xAC, 0x6B, 0x1B, 0xB9, 0x0D, 0x64, 0x65, 0xCA, 0x32,
            0xC6, 0x5C, 0x4C, 0x58, 0x56, 0x7F, 0xF7, 0x48, 0x0C, 0x70},
      .y = {0xAC, 0xA4, 0x5F, 0xB8, 0xCA, 0x82, 0x17, 0x44, 0xE0, 0xDF, 0x40,
            0xF6, 0xFB, 0x46, 0x8D, 0x94, 0xC5, 0xDC, 0x51, 0x5C, 0xBA, 0x20,
            0xDB, 0x0D, 0x06, 0x9B, 0xFD, 0xE3, 0x09, 0xE5, 0x71, 0xDB}};

  auto public_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  ASSERT_TRUE(public_key.has_value());
  UInt256 dhkey = local_key->CalculateDhKey(*public_key);
  const UInt256 kExpectedDhKey{0x7B, 0xBD, 0x97, 0x89, 0x77, 0xD7, 0x0D, 0x04,
                               0x68, 0x1E, 0x56, 0x60, 0x20, 0x85, 0xC5, 0xCC,
                               0x25, 0x2D, 0xDD, 0xFB, 0x34, 0xA4, 0x54, 0x2E,
                               0x01, 0xFF, 0x20, 0x64, 0x10, 0x62, 0xFC, 0x46};
  ASSERT_EQ(kExpectedDhKey, dhkey);
}

TEST(EcdhKeyTest, PublicKeyXAndYComparisonSameKey) {
  // Debug ECDH key given in V5.1 Vol. 3 Part H Section 2.3.5.6.1, converted to
  // little-endian.
  const sm::PairingPublicKeyParams kSerializedKey{
      .x = {0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC, 0xDB, 0xFD, 0xF4,
            0xAC, 0x11, 0x91, 0xF4, 0xEF, 0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83,
            0x2C, 0x5E, 0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x20},
      .y = {0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74, 0x24, 0x30, 0xED,
            0x8F, 0xC2, 0x45, 0x63, 0x76, 0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A,
            0x32, 0x63, 0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0xDC}};
  auto ecdh_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  auto same_ecdh_key = EcdhKey::ParseFromPublicKey(kSerializedKey);
  ASSERT_TRUE(ecdh_key.has_value());
  ASSERT_TRUE(same_ecdh_key.has_value());
  ASSERT_EQ(ecdh_key->GetPublicKeyX(), same_ecdh_key->GetPublicKeyX());
  ASSERT_EQ(ecdh_key->GetPublicKeyY(), same_ecdh_key->GetPublicKeyY());
}

TEST(EcdhKeyTest, PublicKeyXAndYComparisonDifferentKeys) {
  // Debug ECDH key given in V5.1 Vol. 3 Part H Section 2.3.5.6.1, converted to
  // little-endian.
  const sm::PairingPublicKeyParams kSpecSampleSerializedKey{
      .x = {0xE6, 0x9D, 0x35, 0x0E, 0x48, 0x01, 0x03, 0xCC, 0xDB, 0xFD, 0xF4,
            0xAC, 0x11, 0x91, 0xF4, 0xEF, 0xB9, 0xA5, 0xF9, 0xE9, 0xA7, 0x83,
            0x2C, 0x5E, 0x2C, 0xBE, 0x97, 0xF2, 0xD2, 0x03, 0xB0, 0x20},
      .y = {0x8B, 0xD2, 0x89, 0x15, 0xD0, 0x8E, 0x1C, 0x74, 0x24, 0x30, 0xED,
            0x8F, 0xC2, 0x45, 0x63, 0x76, 0x5C, 0x15, 0x52, 0x5A, 0xBF, 0x9A,
            0x32, 0x63, 0x6D, 0xEB, 0x2A, 0x65, 0x49, 0x9C, 0x80, 0xDC}};
  // Test vector taken from NIST ECDH P-256 test vector 0, same as in
  // CalculateDhKeyWorks test.
  const sm::PairingPublicKeyParams kNistSampleSerializedKey{
      .x = {0x87, 0xD2, 0x33, 0x88, 0x83, 0xCC, 0xE7, 0x2C, 0xB4, 0xF6, 0x4D,
            0x3A, 0xCE, 0xAC, 0x6B, 0x1B, 0xB9, 0x0D, 0x64, 0x65, 0xCA, 0x32,
            0xC6, 0x5C, 0x4C, 0x58, 0x56, 0x7F, 0xF7, 0x48, 0x0C, 0x70},
      .y = {0xAC, 0xA4, 0x5F, 0xB8, 0xCA, 0x82, 0x17, 0x44, 0xE0, 0xDF, 0x40,
            0xF6, 0xFB, 0x46, 0x8D, 0x94, 0xC5, 0xDC, 0x51, 0x5C, 0xBA, 0x20,
            0xDB, 0x0D, 0x06, 0x9B, 0xFD, 0xE3, 0x09, 0xE5, 0x71, 0xDB}};

  auto spec_key = EcdhKey::ParseFromPublicKey(kSpecSampleSerializedKey);
  auto nist_key = EcdhKey::ParseFromPublicKey(kNistSampleSerializedKey);
  ASSERT_TRUE(spec_key.has_value());
  ASSERT_TRUE(nist_key.has_value());
  ASSERT_NE(spec_key->GetPublicKeyX(), nist_key->GetPublicKeyX());
  ASSERT_NE(spec_key->GetPublicKeyY(), nist_key->GetPublicKeyY());
}

}  // namespace
}  // namespace bt::sm