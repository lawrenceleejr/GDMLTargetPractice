#ifndef SplitNeutrinoPhysics_h
#define SplitNeutrinoPhysics_h 1

#include "G4VPhysicsConstructor.hh"
#include "globals.hh"
#include <vector>

#include "G4NuMuNucleusCcModel.hh"
#include "G4HadFinalState.hh"
#include "G4HadSecondary.hh"
#include "G4ParticleDefinition.hh"

#include "G4NeutrinoElectronCcModel.hh"
#include "CLHEP/Units/SystemOfUnits.h"

#include "G4NeutrinoElectronNcModel.hh"
#include "G4IonTable.hh"
#include "G4DynamicParticle.hh"

#include "G4VCrossSectionDataSet.hh"
#include "G4NeutrinoElectronNcXsc.hh"
#include "G4NeutrinoElectronCcXsc.hh"
#include "G4Element.hh"
#include "G4Material.hh"
#include "G4Isotope.hh"
#include "G4TauNeutrinoNucleusTotXsc.hh"
#include "G4MuNeutrinoNucleusTotXsc.hh"
#include "G4ElNeutrinoNucleusTotXsc.hh"
#include <cmath>

// Below are templates for the cross sections

template <typename BaseTotXsc> // Our CC cross section
class MyNeutrinoNucleusCcXsc : public BaseTotXsc {
public:
    MyNeutrinoNucleusCcXsc() : BaseTotXsc() {}
    virtual ~MyNeutrinoNucleusCcXsc() = default;

    virtual G4double GetIsoCrossSection(const G4DynamicParticle* aPart, G4int Z, G4int A,  
                                        const G4Isotope* iso, const G4Element* elm, 
                                        const G4Material* mat) override 
    {
        G4double totXsc = BaseTotXsc::GetIsoCrossSection(aPart, Z, A, iso, elm, mat);
        return totXsc * this->GetCcTotRatio();
    }
};

template <typename BaseTotXsc> // Our neutral current cross section
class MyNeutrinoNucleusNcXsc : public BaseTotXsc {
public:
    MyNeutrinoNucleusNcXsc() : BaseTotXsc() {}
    virtual ~MyNeutrinoNucleusNcXsc() = default;

    virtual G4double GetIsoCrossSection(const G4DynamicParticle* aPart, G4int Z, G4int A,  
                                        const G4Isotope* iso, const G4Element* elm, 
                                        const G4Material* mat) override 
    {
        G4double totXsc = BaseTotXsc::GetIsoCrossSection(aPart, Z, A, iso, elm, mat);
        return totXsc * (1.0 - this->GetCcTotRatio());
        //NB: the NC Xsc is definitionally 1-CC
    }
};

// Aliases of the above templates
using MyTauNeutrinoNucleusCcXsc = MyNeutrinoNucleusCcXsc<G4TauNeutrinoNucleusTotXsc>;
using MyTauNeutrinoNucleusNcXsc = MyNeutrinoNucleusNcXsc<G4TauNeutrinoNucleusTotXsc>;
using MyMuNeutrinoNucleusCcXsc  = MyNeutrinoNucleusCcXsc<G4MuNeutrinoNucleusTotXsc>;
using MyMuNeutrinoNucleusNcXsc  = MyNeutrinoNucleusNcXsc<G4MuNeutrinoNucleusTotXsc>;
using MyElNeutrinoNucleusCcXsc  = MyNeutrinoNucleusCcXsc<G4ElNeutrinoNucleusTotXsc>;
using MyElNeutrinoNucleusNcXsc  = MyNeutrinoNucleusNcXsc<G4ElNeutrinoNucleusTotXsc>;

class SplitNeutrinoPhysics : public G4VPhysicsConstructor
{
public:
    SplitNeutrinoPhysics(const G4String& name = "SplitNeutrinoPhysics");
    virtual ~SplitNeutrinoPhysics() override = default;

    virtual void ConstructParticle() override {} // Particles constructed by reference physics list
    virtual void ConstructProcess() override;

private:
    // Sector-specific construction helpers
    void ConstructNuElectronProcesses();
    void ConstructNuNucleusProcesses();

    // Helper to cleanly register new processes and purge deprecated ones across all neutrino species
    void RegisterProcessesForNeutrinos(
        const std::vector<G4String>& neutrinos,
        const std::vector<G4VProcess*>& newProcesses,
        const std::vector<G4String>& processesToRemove = {}
    );
};

/* NB: We don't need to check dp to make sure the dynamic particle is a
    neutrino since our biasing wrapper is already coded to only attach to
    neutrinos, so we can safely leve out dp in the calls to
    `IsElementApplicable` and `IsIsoApplicable`.*/

template <typename BaseXsc>
class MyNeutrinoElectronXsc : public BaseXsc {
public:
    MyNeutrinoElectronXsc() : BaseXsc() { this->SetForAllAtomsAndEnergies(true); }
    virtual ~MyNeutrinoElectronXsc() = default;

    virtual G4bool IsElementApplicable(const G4DynamicParticle*, G4int Z, const G4Material* = nullptr) override { return (Z >= 1); }
    virtual G4bool IsIsoApplicable(const G4DynamicParticle*, G4int Z, G4int, const G4Element* = nullptr, const G4Material* = nullptr) override { return (Z >= 1); }
    virtual G4double GetElementCrossSection(const G4DynamicParticle* dp, G4int Z, const G4Material* mat = nullptr) override {
        if (!BaseXsc::IsElementApplicable(dp, Z, mat)) return 0.0;
        return BaseXsc::GetElementCrossSection(dp, Z, mat);
    }
    virtual G4double GetIsoCrossSection(const G4DynamicParticle* dp, G4int Z, G4int, const G4Isotope* = nullptr, const G4Element* = nullptr, const G4Material* mat = nullptr) override {
        return this->GetElementCrossSection(dp, Z, mat);
    }
};

template <typename BaseModel>
class MyNuElectronModel : public BaseModel {
public:
    MyNuElectronModel() : BaseModel() {}
    virtual G4HadFinalState* ApplyYourself(const G4HadProjectile& aTrack, G4Nucleus& targetNucleus) override {
        G4HadFinalState* result = BaseModel::ApplyYourself(aTrack, targetNucleus);
        if (result) {
            G4int Z = targetNucleus.GetZ_asInt();
            G4int A = targetNucleus.GetA_asInt();
            if (auto ion = G4IonTable::GetIonTable()->GetIon(Z, A, 0.0)) {
                result->AddSecondary(new G4DynamicParticle(ion, G4ThreeVector(0,0,1), 0.0));
            }
        }
        return result;
    }
};

using MyNuElectronCcModel = MyNuElectronModel<G4NeutrinoElectronCcModel>;
using MyNuElectronNcModel = MyNuElectronModel<G4NeutrinoElectronNcModel>;

using MyNeutrinoElectronCcXsc = MyNeutrinoElectronXsc<G4NeutrinoElectronCcXsc>;
using MyNeutrinoElectronNcXsc = MyNeutrinoElectronXsc<G4NeutrinoElectronNcXsc>;


/* I leave this in as an example of how to subclass the models to implement
stricter checks than Geant4 typically allows. It is not used in MAIA studies as
those do not rely on Geant4 neutrino event generation at all, and comparisons
against other neutrino event generators are to be done with Geant4 being as
"out of the box" as is appropriate for the standard application developer, which
does not include this level of posthoc fixes.*/
class MyNuMuNucleusCcModel : public G4NuMuNucleusCcModel {
public:
    MyNuMuNucleusCcModel() : G4NuMuNucleusCcModel() {}
    // Have to subclass our models because G4 does not handle energy, momentum,
    // and charge conservation very thoroughly.

    virtual const std::pair<G4double, G4double> GetFatalEnergyCheckLevels() const override {
        // 0.02% relative, 10 MeV absolute, for some reason I still get ~50 MeV
        // momentum imbalances. I don't understand why this happens, but altering
        // the strictness of the checks below does indeed make noticable changes
        // to the accuracy of the output momentum as compared to the input
        return std::pair<G4double, G4double>(0.0002, 10.0 * CLHEP::MeV);
    }

    virtual G4HadFinalState* ApplyYourself(const G4HadProjectile& aTrack, G4Nucleus& targetNucleus) override {
        G4HadFinalState* result = nullptr;
        bool isConserved = false;
        
        G4int attempts = 0;
        const G4int maxAttempts = 1000; // Prevent infinite loops if the model gets stuck

        // Target and Projectile initial properties
        G4double initialCharge = aTrack.GetDefinition()->GetPDGCharge() + targetNucleus.GetZ_asInt();
        G4ThreeVector initialMomentum = aTrack.Get4Momentum().vect();

        while (!isConserved && attempts < maxAttempts) {
            attempts++;

            // Call the original model to generate an event
            // Note: The base class calls theParticleChange.Clear() internally, 
            // so we do not need to worry about secondaries piling up between attempts.
            result = G4NuMuNucleusCcModel::ApplyYourself(aTrack, targetNucleus);

            // If the neutrino bypassed the interaction (e.g. below threshold, cascade failed),
            // it will have no secondaries. We just pass it through immediately.
            if (result->GetNumberOfSecondaries() == 0) {
                return result;
            }

            // Tally the final state
            G4double finalCharge = 0.0;
            G4ThreeVector finalMomentum(0., 0., 0.);

            for (std::size_t i = 0; i < result->GetNumberOfSecondaries(); ++i) {
                G4DynamicParticle* sec = result->GetSecondary(i)->GetParticle();
                finalCharge += sec->GetDefinition()->GetPDGCharge();
                finalMomentum += sec->GetMomentum();
            }

            // 4. Check Conservation
            G4double chargeDiff = std::abs(initialCharge - finalCharge);
            G4double momentumDiff = (initialMomentum - finalMomentum).mag();

            // Strict constraints: exact charge match, 100 MeV/c momentum match
            bool chargeConserved = (chargeDiff < 0.1); 
            bool momentumConserved = (momentumDiff < 100.0 * CLHEP::MeV);
            // If we tighten the momentum conservation much more, G4 has a very
            // hard time satisfying our request. This is because G4 has very
            // poor momentum bookkeping in these neutrino interactions, so we
            // are essentially asking it to guess the right answer.

            if (chargeConserved && momentumConserved) {
                isConserved = true;
            }
        }

        // If the model cannot generate a valid event after 1000 tries,
        // we veto the interaction entirely.
        if (!isConserved) {
            result->Clear(); // Wipe the bad secondaries
            result->SetStatusChange(isAlive); // Keep the neutrino alive
            result->SetEnergyChange(aTrack.GetKineticEnergy()); 
            result->SetMomentumChange(aTrack.Get4Momentum().vect().unit());
        }

        return result;
    }
};

#endif